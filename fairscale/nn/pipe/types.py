from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from dataclasses import dataclass
import torch
from torch import Tensor, nn

ACTIVATIONS_GRADS_QUEUE = 0
SKIP_TENSOR_QUEUE = 1
PORTAL_QUEUE = 2
EVENT_LOOP_QUEUE = 3
MESSAGE_GENERATION_START = 4

MessageGeneration = MESSAGE_GENERATION_START

Tensors = Tuple[Tensor, ...]
TensorOrTensors = Union[Tensor, Tensors]

InputDevice = Union[None, int, str, torch.device]
Schedule = List[Tuple[int, int]]


class LazyModule:
    def __init__(self, function: Callable[[], nn.Module]):
        self.function = function

    def __call__(self) -> nn.Module:
        return self.function()


class PipelineStyle(Enum):
    SingleProcess = auto()
    MultiProcess = auto()
    AsyncSchedule = auto()


@dataclass(frozen=True)
class TransportConfig:
    use_rpc: bool
    worker_map: Optional[Dict[int, str]]


@dataclass(init=False)
class PipeMessage:
    src: int
    dest: int
    queue_name: int
    args: Any
    tensors: Tensors
    tensor_shapes: List[torch.Size]
    tensor_dtypes: List[torch.dtype]
    tag: int = 0

    def __init__(self, src: int, dest: int, queue_name: int, args: Any, tensors: Tensors):
        self.src = src
        self.dest = dest
        self.queue_name = queue_name
        self.args = args
        self.tensors = tensors
        self.tensor_shapes = []
        self.tensor_dtypes = []

        global MessageGeneration
        self.tag = MessageGeneration
        MessageGeneration += len(tensors)