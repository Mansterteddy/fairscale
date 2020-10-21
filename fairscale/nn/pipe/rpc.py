import os
from threading import Event, Lock, Thread
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

import torch
from torch import nn
from torch.distributed import ProcessGroup, rpc
from torch.distributed.distributed_c10d import _get_global_rank

from fairscale.nn.model_parallel.initialize import get_pipeline_parallel_group

from . import Pipe
from .messages import recv_message_tensors, send_message
from .types import EVENT_LOOP_QUEUE, PipeMessage, TensorOrTensors, TransportConfig

DEFAULT_MAX_SOURCE_POSITIONS = 1024
DEFAULT_MAX_TARGET_POSITIONS = 1024

PipeModel: Pipe


SizeOrSizes = Union[torch.Size, List[torch.Size]]
DtypeOrDtypes = Union[torch.dtype, List[torch.dtype]]


def dprint(s: str) -> None:
    print(str(torch.distributed.get_rank()) + ": " + s)


def set_device_based_on_group(group: ProcessGroup) -> None:
    # torch.cuda.set_device(group.rank() % torch.cuda.device_count())
    torch.cuda.set_device(torch.distributed.get_rank() % torch.cuda.device_count())


def register_remote_model(args: List[Any], kwargs: Dict[str, Any]) -> None:
    group = get_pipeline_parallel_group()  # FIXME(tom) handle dynamic group
    set_device_based_on_group(group)
    dprint(f"model registered {torch.cuda.current_device()}")
    kwargs["group"] = group
    kwargs["input_device"] = torch.device("cuda", torch.cuda.current_device())
    model = Pipe(*args, **kwargs)
    model.cuda()
    globals()["PipeModel"] = model


def get_shapes(tensor: TensorOrTensors) -> SizeOrSizes:
    if isinstance(tensor, torch.Tensor):
        return tensor.shape
    else:
        return [t.shape for t in tensor]


def get_dtype(tensor: TensorOrTensors) -> DtypeOrDtypes:
    if isinstance(tensor, torch.Tensor):
        return tensor.dtype
    else:
        return [t.dtype for t in tensor]


def model_forward(training: bool, shape: torch.Size, dtype: torch.dtype) -> Optional[Tuple[SizeOrSizes, DtypeOrDtypes]]:
    try:
        dprint(f"mf: train stage {torch.distributed.get_rank()}")
        if isinstance(shape, torch.Size):
            tensor = torch.empty(shape, dtype=dtype)
        else:
            tensor = tuple([torch.empty(s, dtype=d) for s, d in zip(shape, dtype)])

        model = globals()["PipeModel"]
        set_device_based_on_group(model.group)

        dprint(f"mf: train stage {model.group.rank()}, {os.getpid()}")
        model.train(training)
        result = model(tensor)
        if model.final_stage:
            globals()["PipeResult"] = result
            return (get_shapes(result), get_dtype(result))
    except Exception as e:
        print(f"failboat {e} {type(e)}")
        import traceback

        print(f"format {traceback.format_exc()}")
        raise e

    return None


def send_result(training: bool, message: PipeMessage, grads_message: PipeMessage) -> None:
    dprint(f"send result {training}")
    group = get_pipeline_parallel_group()
    set_device_based_on_group(group)
    try:
        dprint(f"send result {torch.distributed.get_rank()}, {torch.cuda.current_device()}")
        result = globals()["PipeResult"]
        model = globals()["PipeModel"]

        if isinstance(result, torch.Tensor):
            result = [result]

        dest = _get_global_rank(group, 0)

        print(
            f"ho har {torch.distributed.get_rank()} " + str([_get_global_rank(group, i) for i in range(group.size())])
        )
        message.tensors = tuple(result)
        config = TransportConfig(False, None)
        send_message(config, message, sync=False, skip_header=True)

        if training:
            grads_message.tensor_shapes = [r.shape for r in result]
            grads_message.tensor_dtypes = [r.dtype for r in result]
            input_device = torch.device("cuda", torch.cuda.current_device())
            transport_config = TransportConfig(False, None)
            grads_message = recv_message_tensors(input_device, transport_config, grads_message)

            with model.lock:
                print(f" >>> autograd-backward tail")
                torch.autograd.backward(result, grads_message.tensors, retain_graph=True)
                print(f" <<< autograd-backward tail")

    except Exception as e:
        print(f"got {e}")


def recv_result(shapes: SizeOrSizes, dtypes: DtypeOrDtypes, message: PipeMessage) -> TensorOrTensors:
    group = get_pipeline_parallel_group()
    set_device_based_on_group(group)
    src = torch.distributed.distributed_c10d._get_global_rank(group, group.size() - 1)
    dprint(f"recv_result... {src}, {torch.cuda.current_device()}")

    input_device = torch.device("cuda", torch.cuda.current_device())
    transport_config = TransportConfig(False, None)

    if isinstance(shapes, torch.Size):
        shape = cast(torch.Size, shapes)
        dtype = cast(torch.dtype, dtypes)
        message.tensor_shapes = [shape]
        message.tensor_dtypes = [dtype]
        message = recv_message_tensors(input_device, transport_config, message)
        return message.tensors[0]
    else:
        shapes = cast(List[torch.Size], shapes)
        dtypes = cast(List[torch.dtype], dtypes)
        message.tensor_shapes = shapes
        message.tensor_dtypes = dtypes
        message = recv_message_tensors(input_device, transport_config, message)
        return message.tensors


def get_global_ranks_from_group(group: ProcessGroup) -> List[int]:
    return [torch.distributed.distributed_c10d._get_global_rank(group, r) for r in range(group.size())]


def run_model(model: Pipe, tensor: TensorOrTensors, event: Event, lock: Lock) -> None:
    t = model.training
    with lock:
        print(f">> run_model thread {t}")
        assert model.group
        set_device_based_on_group(model.group)
        model(tensor, event=event)
        print(f"<< run_model thread {t}")


class PipeBackRedirect(torch.autograd.Function):
    @staticmethod
    # type: ignore
    def forward(ctx, inputs, dest, event, message):
        ctx.dest = dest
        ctx.event = event
        ctx.message = message
        return inputs

    @staticmethod
    # type: ignore
    def backward(ctx, *grad):
        dprint(f">>> back hook yay")
        config = TransportConfig(False, None)
        ctx.message.tensors = tuple(grad)
        send_message(config, ctx.message, sync=False, skip_header=True)
        ctx.event.set()
        dprint(f"<<< back hook yay")
        return (None, None, None, None)


def callback_with_model(callback: Callable, ctx: Any) -> None:
    group = get_pipeline_parallel_group()  # FIXME(tom) handle dynamic group
    set_device_based_on_group(group)

    global PipeModel

    with PipeModel.lock:
        callback(ctx, PipeModel)


class PipeRPCWrapper(nn.Module):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__()
        self.group = cast(ProcessGroup, kwargs.get("group")) or get_pipeline_parallel_group()
        assert self.group.rank() == 0
        self.lock = Lock()

        if True:
            assert (
                self.group == get_pipeline_parallel_group()
            ), "Can't pickle groups, so group must be `get_pipeline_parallel_group()`"
            kwargs["group"] = None
        else:
            kwargs["group"] = self.group

        kwargs["style"] = Pipe.AsyncSchedule
        kwargs["input_device"] = torch.device("cuda", torch.cuda.current_device())

        self.model = Pipe(*args, **kwargs)
        self.worker_map = kwargs["worker_map"]
        futures = [
            # FIXME get global rank
            rpc.rpc_async(self.get_rpc_name(rank), register_remote_model, args=(args, kwargs))
            for rank in range(1, self.group.size())
        ]
        futures = [f.wait() for f in futures]
        self.model.cuda()

    def get_rpc_name(self, rank: int) -> str:
        return self.worker_map[_get_global_rank(self.group, rank)]

    def foreach_worker(self, callback: Callable, ctx: Any = None, *, include_self: bool = False) -> None:
        futures = [
            rpc.rpc_async(self.get_rpc_name(rank), callback_with_model, args=(callback, ctx))
            for rank in range(1, self.group.size())
        ]
        futures = [f.wait() for f in futures]
        if include_self:
            with self.model.lock:
                callback(ctx, self.model)

    def forward(self, tensor: TensorOrTensors) -> TensorOrTensors:  # type: ignore
        shape = get_shapes(tensor)
        dtype = get_dtype(tensor)

        if isinstance(tensor, torch.Tensor):
            num_tensors = 1
        else:
            num_tensors = len(tensor)

        futures = [
            rpc.rpc_async(self.get_rpc_name(rank), model_forward, args=(self.model.training, shape, dtype))
            for rank in range(1, self.group.size())
        ]

        if self.model.final_stage:
            return self.model(tensor)
        else:
            event = Event()
            t = Thread(target=run_model, args=(self.model, tensor, event, self.lock))
            t.start()

            dprint("forward before wait recv")
            shape, dtype = futures[-1].wait()
            dprint("forward after wait recv")
            dest_rank = self.group.size() - 1
            dest = self.get_rpc_name(dest_rank)
            dest_global_rank = _get_global_rank(self.group, dest_rank)
            src_global_rank = torch.distributed.get_rank()
            dprint(f"async to {dest}")
            queue = EVENT_LOOP_QUEUE
            message = PipeMessage(dest_global_rank, src_global_rank, queue_name=queue, tensor_count=num_tensors)
            grads_message = PipeMessage(src_global_rank, dest_global_rank, queue_name=queue, tensor_count=num_tensors)
            rpc.rpc_async(dest, send_result, args=(self.model.training, message, grads_message))
            dprint(">>> recv_result")
            result = recv_result(shape, dtype, message)
            dprint("<<< recv_result")
            # event.set()
            dprint("not set event")
            try:
                if isinstance(result, torch.Tensor):
                    result.requires_grad_()
                else:
                    for r in result:
                        r.requires_grad_()

                applied = PipeBackRedirect.apply(result, _get_global_rank(self.group, dest_rank), event, grads_message)
            except Exception as e:
                dprint(f"failed got {e}")
            dprint("return applied")
            return applied

    @property
    def final_stage(self) -> bool:
        return self.model.final_stage
