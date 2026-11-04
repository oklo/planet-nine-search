from torch.nn import functional as F


def local_contrast(score):
    """Annular contrast, not a tail probability; inner seven pixels excluded."""
    x = score[:, None]
    count = 15**2 - 7**2
    mean = (F.avg_pool2d(x, 15, 1, 7) * 225 - F.avg_pool2d(x, 7, 1, 3) * 49) / count
    square = (F.avg_pool2d(x * x, 15, 1, 7) * 225 - F.avg_pool2d(x * x, 7, 1, 3) * 49) / count
    return ((x - mean) / (square - mean**2).clamp_min(0.04).sqrt())[:, 0]
