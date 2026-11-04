"""Select one compute device for a worker process."""

import torch

_device = torch.device("cpu")


def configure(name="auto", threads=4):
    global _device
    if threads < 1:
        raise ValueError("threads must be positive")
    if name == "auto":
        name = (
            "cuda"
            if torch.cuda.is_available()
            else ("mps" if torch.backends.mps.is_available() else "cpu")
        )
    if name not in {"cpu", "mps", "cuda"}:
        raise ValueError("device must be auto, cpu, mps, or cuda")
    if name == "mps" and not torch.backends.mps.is_available():
        raise ValueError("MPS is unavailable")
    if name == "cuda" and not torch.cuda.is_available():
        raise ValueError("CUDA is unavailable")
    torch.set_num_threads(threads)
    _device = torch.device(name)
    return str(_device)


def device():
    return _device


def release():
    if _device.type == "mps":
        torch.mps.empty_cache()
    elif _device.type == "cuda":
        torch.cuda.empty_cache()
