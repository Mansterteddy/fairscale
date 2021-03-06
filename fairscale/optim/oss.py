# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

from collections import OrderedDict
import copy
from itertools import chain
import logging
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Type

import torch
import torch.distributed as dist
from torch.nn import Parameter
from torch.optim import SGD, Optimizer

from .utils import broadcast_object, recursive_copy_to_device

__all__ = ["OSS"]

if TYPE_CHECKING:  # pragma: no cover
    from torch.optim.optimizer import _params_t
else:
    _params_t = Any


class OSS(Optimizer):
    """Wraps an arbitrary :class:`optim.Optimizer <torch.optim.Optimizer>`
    optimizer and shards its state as described by ZeRO_.
    ::

        opt = OSS(params, optim=torch.optim.Adam, lr=0.01)

    .. _ZeRO: https://arxiv.org/abs/1910.02054

    We use a greedy algorithm to pack a number of parameters
    at each rank. Each parameter belongs to a single rank and
    is not divided among rank.

    After each rank completed their parameter update, they broadcast
    the new version of the parameters to all other ranks to synchronize
    the parameters for next round forward/backward computation.

    Args:
        params (list of tensors):
            parameters to be optimized
    Keyword Args:
        optim (torch.nn.Optimizer):
            optimizer to shard (default: SGD)
        group (group):
            torch.distributed group (default: group.WORLD)
        broadcast_buffer_size (int):
            the size of the buffer used to batch the small parameter tensors (default 128k).
    """

    #: The optimizer used for a given shard
    optim: Optimizer

    in_super_constructor: bool

    def __init__(
        self,
        params: _params_t,
        optim: Type[Optimizer] = SGD,
        group: Optional[Any] = None,
        broadcast_buffer_size: int = 2 ** 17,
        **default: Any,
    ):
        # Hold all the model params in the root .param_groups
        self.in_super_constructor = True
        super().__init__(params, default)
        self.in_super_constructor = False

        # Partition information. lazy evaluation, computed if requested
        self._per_device_params: OrderedDict[
            torch.device, List[List[Parameter]]
        ] = OrderedDict()  # device, rank, params
        self._param_rank: Dict[torch.Tensor, int] = {}
        self._partition_parameters: List[List[dict]] = []

        # Build the wrapped optimizer, responsible for a shard of the params
        self.group = group if group is not None else dist.group.WORLD
        self.world_size = dist.get_world_size(self.group)
        self.rank = dist.get_rank(self.group)
        self.global_rank = self.get_global_rank(self.group, self.rank)

        self.optim = optim(self.partition_parameters()[self.rank], **default)

        # - Sync local and global param_groups keys
        for global_group, local_group in zip(self.param_groups, self.optim.param_groups):
            for k, v in local_group.items():
                if k != "params":
                    global_group[k] = v

        #  Optional consolidated optimizer state
        self._all_states: List[Dict[str, Any]] = []

        # Current default device is set by the parameters allocated to this rank
        self._device = self.partition_parameters()[self.rank][0]["params"][0].device
        self._broadcast_buffers: Dict[torch.device, List[torch.Tensor]] = {}
        for device, per_device in self.per_device_params.items():
            # Allocate one buffer per rank and per device to group the small parameters
            self._broadcast_buffers[device] = [
                torch.zeros(broadcast_buffer_size, dtype=per_device[0][0].dtype, device=device)
                for _ in range(len(per_device))
            ]

    # Partition helpers
    def partition_parameters(self) -> List[List[dict]]:
        """Partitions parameters across distributed data parallel ranks.

        Returns a list of param_groups (which is a list of dict) where each
        element of the list contains the param_groups for a rank. Element 0
        corresponds to rank 0, etc. We need all the ranks for the broadcast
        inside step().
        """
        if len(self._partition_parameters) == 0:
            self._partition_parameters = [list() for _ in range(self.world_size)]
            sizes = [0] * self.world_size
            for param_group in self.param_groups:
                param_lists: List[List] = [list() for _ in range(self.world_size)]
                for param in param_group["params"]:
                    # Add this param to rank with smallest size.
                    rank = sizes.index(min(sizes))
                    param_lists[rank].append(param)
                    sizes[rank] += param.numel()

                for rank, params in enumerate(param_lists):
                    param_group_rank = copy.copy(param_group)
                    param_group_rank["params"] = params
                    self._partition_parameters[rank].append(param_group_rank)

        return self._partition_parameters

    @property
    def per_device_params(self) -> Dict[torch.device, List[List[Parameter]]]:
        """Sorted list of all the params, first per device then per rank.

        Within a list params are sorted per number of elements to allow for an easy bucketing.
        """
        if len(self._per_device_params) == 0:
            # Go through all params, log them per device
            # The ordering is important here, needs to be the same on all ranks
            # So that ulterior broadcast calls are matching
            for param_group in self.param_groups:
                for param in param_group["params"]:
                    device = param.device
                    if self._per_device_params.get(device) is None:
                        self._per_device_params[device] = [[] for _ in range(self.world_size)]
                    self._per_device_params[device][self.param_to_rank[param]] += [param]

            # Sort param_lists by size
            for k in self._per_device_params.keys():
                for r in self._per_device_params[k]:
                    r.sort(key=lambda x: x.numel())

        return self._per_device_params

    @property
    def param_to_rank(self) -> Dict[torch.Tensor, int]:
        """param to data parallel rank"""
        if len(self._param_rank) == 0:
            for rank, param_groups in enumerate(self.partition_parameters()):
                for param_group in param_groups:
                    for param in param_group["params"]:
                        self._param_rank[param] = rank
        return self._param_rank

    # NOTE(msb) We add a kwargs in order to support Optimizer sub-classes that support extra kwargs.
    # For example, the apex library contains fused optimizers with a step that supports extra kwargs.
    def step(self, closure: Optional[Callable[[], float]] = None, **kwargs: Any) -> Optional[float]:
        """Performs a single optimization step (parameter update).

        Arguments:
            closure (callable): A closure that reevaluates the model and
                returns the loss. Optional for most optimizers.

        .. note: Any extra parameter is passed to the base optimizer as-is"""

        # Sync oss param_groups attributes in case they've been updated by a scheduler.
        self._sync_param_groups()

        # Run the optimizer step on this shard only:
        self._free_other_grads()

        if closure is not None:
            loss = self.optim.step(closure=closure, **kwargs)  # type: ignore
        else:
            loss = self.optim.step(**kwargs)

        # Sync all the updated shards in between the ranks
        with torch.no_grad():
            for (
                device,
                device_params,
            ) in self.per_device_params.items():  # all the params on this device (inc all ranks)
                self._broadcast_params(self._broadcast_buffers[device], device_params)

        # Sync hypothethical new results from the wrapped optimizer to the exposed param_groups
        self._sync_param_groups(local_to_global=True)

        return loss

    def local_state_dict(self) -> dict:
        """Gets this rank's state_dict.

        Returns:
            The state of the optimizer as a :class:`dict`.
            It contains two entries:

            * state - a dict holding current optimization state. Its content
                differs between optimizer classes.
            * param_groups - a dict containing all parameter groups
        """
        return self.optim.state_dict()

    def consolidate_state_dict(self, recipient_rank: int = 0) -> None:
        """Update the consolidated state_dict list, one per rank.

        .. warning: This needs to be called on all replicas"""

        # Sync lr and other attributes in case its been updated
        self._sync_param_groups()

        if self.rank == recipient_rank:
            # Pull the sharded state from all the other replicas
            # Store all the states in order, rank by rank
            logging.debug("Pulling the sharded optimizer state from all replicas")
            self._all_states = self._collect_sharded_states()
        else:
            # Acknowledge broadcasts, and send this rank's shard when needed
            self._broadcast_state_dict()

    def state_dict(self) -> Dict[str, Any]:
        """Return the last known global optimizer state, which consist of a list of the shards.

        .. warning:
            If the state has not been consolidated, this returns a shard's worth, not the global state.

        .. warning:
            Returning the global state is limited to the replica which was responsible for the consolidation.
            The state may also not be up to date, depending on when `consolidate_state_dict` was last called.
        """

        if len(self._all_states) == 0:
            logging.warning("Optimizer state has not been consolidated. Returning the local state")
            logging.warning("Please call `consolidate_state_dict()` beforehand if you meant to save the global state")
            state_dict = self.local_state_dict()
            state_dict["local_state_dict"] = True
            return state_dict

        # Flatten the param_groups, save the partition which logs the rank <> shard correspondence
        partition: List[Tuple[int, int]] = []
        param_groups: List[Dict[Any, Any]] = []

        start = 0
        for i, s in enumerate(self._all_states):
            param_groups.extend(s["param_groups"])
            end = start + len(s["param_groups"])
            partition.append((start, end))
            start = end

        return {
            "state": [s["state"] for s in self._all_states],
            "param_groups": param_groups,
            "partition": partition,
            "local_state_dict": False,
        }

    @staticmethod
    def rank_local_state_dict(rank: int, state_dict: dict) -> dict:
        """Returns the local_state_dict for a given rank.

        Arguments:
            rank (int): rank to get local_state_dict for
            state_dict (dict): global state_dict
        """
        # Get this optimizer's param_groups shard
        param_groups = state_dict["param_groups"][state_dict["partition"][rank][0] : state_dict["partition"][rank][1]]
        return {"state": state_dict["state"][rank], "param_groups": param_groups}

    def load_local_state_dict(self, state_dict: dict) -> None:
        """Loads this rank's state_dict.

        .. warning: This is not meant to load the global state dict.
        """

        self.optim.load_state_dict(state_dict)

        # Workaround PyTorch bug that casts state (https://github.com/pytorch/pytorch/issues/43706)
        # Copied from https://github.com/pytorch/fairseq/blob/v0.9.0/fairseq/optim/fp16_optimizer.py#L251-L268
        groups = self.optim.param_groups
        saved_groups = state_dict["param_groups"]
        id_map = {
            old_id: p
            for old_id, p in zip(chain(*(g["params"] for g in saved_groups)), chain(*(g["params"] for g in groups)))
        }
        for k, v in state_dict["state"].items():
            if k in id_map:
                param = id_map[k]
                self.optim.state[param] = recursive_copy_to_device(v, non_blocking=True, device=param.device)

        # Restore the global param_groups (the params themselves are already correct)
        for global_group, local_group in zip(self.param_groups, groups):
            for k, v in local_group.items():
                if k != "params":
                    global_group[k] = v

    def load_state_dict(self, state_dict: Dict[str, Any]) -> None:
        """Restore the global parameter groups as well as the shard.

        Arguments:
            state_dict (dict): optimizer state. Should be an object returned
                from a call to :meth:`state_dict`
        """

        # Check whether we got a local or global dict
        if state_dict["local_state_dict"]:
            self.load_local_state_dict(state_dict)
        else:
            # Dispatch this rank's state dictionary to the wrapped shard optimizer
            self.load_local_state_dict(OSS.rank_local_state_dict(self.rank, state_dict))

    def add_param_group(self, param_group: dict) -> None:
        """Add a param group to the :class:`Optimizer` s `param_groups`.

        This can be useful when fine tuning a pre-trained network as frozen layers can be made
        trainable and added to the :class:`Optimizer` as training progresses.

        Arguments:
            param_group (dict): Specifies what Tensors should be optimized along with group
            specific optimization options

        .. warning: This handles updating the shards on all partitions, but needs to be called on all ranks.
        """

        super().add_param_group(param_group)
        if not self.in_super_constructor:
            # Force a re-partitioning
            self._partition_parameters.clear()
            self._per_device_params.clear()
            self._param_rank.clear()

            param_groups = self.partition_parameters()[self.rank]
            if len(param_groups) == len(self.optim.param_groups) + 1:
                self.optim.add_param_group(param_groups[-1])

    def _sync_param_groups(self, local_to_global: bool = False) -> None:
        """Sync learning rate and other optimizer attributes (needed to support schedulers).
        If the global param groups have been altered, and we want to make sure that the
        wrapped optimizer uses the up to date version.
        Conversely if the wrapped optimizer has new keys, we expose them through the global param groups"""

        for global_group, local_group in zip(self.param_groups, self.optim.param_groups):
            # Sync everything but the parameters
            for k in filter(lambda x: x != "params", local_group.keys()):
                if local_to_global:
                    global_group[k] = local_group[k]
                elif k in global_group.keys():
                    local_group[k] = global_group[k]

    def _collect_sharded_states(self) -> List[Dict[str, Any]]:
        """Collect all the state shards, in CPU memory."""
        empty_buffer = torch.tensor([0], dtype=torch.uint8, device=self._device)
        all_states: List[Dict[str, Any]] = []

        for rank in range(self.world_size):
            if rank == self.rank:
                logging.debug("Saving self state")
                all_states.append(
                    recursive_copy_to_device(self.local_state_dict(), non_blocking=True, device=torch.device("cpu"))
                )

                # Sync with other replicas
                broadcast_object(empty_buffer, src_rank=self.global_rank, group=self.group, dist_device=self._device)
            else:
                # Fetch the optim state from the other replicas
                global_rank = self.get_global_rank(self.group, rank)
                replica_state = broadcast_object(
                    empty_buffer, src_rank=global_rank, group=self.group, dist_device=self._device
                )

                all_states.append(
                    recursive_copy_to_device(replica_state, non_blocking=True, device=torch.device("cpu"))
                )

                logging.debug("State from rank %s received", rank)

        return all_states

    def _broadcast_state_dict(self) -> None:
        """Broadcast this rank's state shard, discard others"""
        empty_buffer = torch.tensor([0], dtype=torch.uint8, device=self._device)

        for rank in range(self.world_size):
            if rank == self.rank:
                # Send the state to the reference replica
                logging.debug(
                    "Sending the sharded optimizer state to the reference replica from rank %s", rank,
                )
                broadcast_object(
                    self.local_state_dict(), src_rank=self.global_rank, group=self.group, dist_device=self._device
                )
            else:
                global_rank = self.get_global_rank(self.group, rank)
                # Discard this tensor/rank, broadcast necessary for syncing
                broadcast_object(empty_buffer, src_rank=global_rank, group=self.group, dist_device=self._device)

    def _free_other_grads(self) -> None:
        """Free all the gradients only useful for the other ranks
        """
        for rank, partition in enumerate(self.partition_parameters()):
            if rank == self.rank:
                continue

            for p in partition:
                for t in p["params"]:
                    t.grad = None

    @staticmethod
    def get_global_rank(group: Any, rank: int) -> int:
        if group is dist.group.WORLD:
            return rank
        else:
            global_rank = dist.distributed_c10d._get_global_rank(group, rank)  # type: ignore
        return global_rank

    def _broadcast_params(self, buffers: List[torch.Tensor], per_rank_params: List[List[Parameter]]) -> None:
        """Helper function to broadcast all the parameters from a given device"""
        buffer_size = buffers[0].numel()
        bucket_requests = []
        direct_requests = []

        # Bucket and issue all the async calls
        for (src_rank, params), buffer in zip(enumerate(per_rank_params), buffers):
            global_src_rank = self.get_global_rank(self.group, src_rank)

            # Copy small parameters into per-GPU buffers and then async broadcast
            offset = 0
            bucket_sent = False
            bucket_params = []

            # All the params are sorted per rank and per increasing size
            for p in params:
                # Since all the parameters are already sorted per increasing size, we only need to consider the first ones.
                if not bucket_sent and offset + p.numel() < buffer_size:
                    end = offset + p.numel()
                    buffer[offset:end].copy_(p.data.view(-1))
                    bucket_params.append((p, offset, end))
                    offset = end
                else:
                    if offset > 0 and not bucket_sent:
                        bucket_requests.append(
                            (
                                dist.broadcast(tensor=buffer, src=global_src_rank, group=self.group, async_op=True),
                                src_rank,
                                bucket_params,
                            )
                        )

                        bucket_sent = True

                    direct_requests.append(
                        dist.broadcast(tensor=p.data, src=global_src_rank, group=self.group, async_op=True)
                    )

            # Catch a trailing bucket
            if not bucket_sent:
                bucket_requests.append(
                    (
                        dist.broadcast(tensor=buffer, src=global_src_rank, group=self.group, async_op=True),
                        src_rank,
                        bucket_params,
                    )
                )

        # Unroll the initial packed small parameters
        for work_handle, src_rank, bucket_params in bucket_requests:
            work_handle.wait()
            if src_rank != self.rank:
                for p, offset, end in bucket_params:
                    p.data.copy_(buffers[src_rank][offset:end].view_as(p.data))

        # Unroll all the async work items, just in case
        _ = list(map(lambda x: x.wait(), direct_requests))
