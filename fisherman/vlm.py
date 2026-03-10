"""Moondream 2B scene understanding — lazy-loaded, runs on MPS or CPU."""

import io

import structlog

log = structlog.get_logger()

_model = None


def _patch_sdpa_for_gqa():
    """Monkey-patch scaled_dot_product_attention to support enable_gqa on
    older PyTorch (< 2.5) by manually expanding K/V heads."""
    import torch
    import torch.nn.functional as F

    _orig_sdpa = F.scaled_dot_product_attention

    # Check if the original already supports enable_gqa by trying a dummy call
    try:
        import inspect
        sig = inspect.signature(_orig_sdpa)
        if "enable_gqa" in sig.parameters:
            return  # Nothing to patch
    except (ValueError, TypeError):
        # Built-in C function — check torch version instead
        if tuple(int(x) for x in torch.__version__.split("+")[0].split(".")[:2]) >= (2, 5):
            return  # torch >= 2.5 supports enable_gqa

    def _patched_sdpa(query, key, value, attn_mask=None, dropout_p=0.0,
                      is_causal=False, scale=None, enable_gqa=False):
        if enable_gqa and query.shape[1] != key.shape[1]:
            n_rep = query.shape[1] // key.shape[1]
            key = key.repeat_interleave(n_rep, dim=1)
            value = value.repeat_interleave(n_rep, dim=1)
        return _orig_sdpa(query, key, value, attn_mask=attn_mask,
                          dropout_p=dropout_p, is_causal=is_causal, scale=scale)

    F.scaled_dot_product_attention = _patched_sdpa
    log.info("sdpa_patched_for_gqa")


def _load_model(revision: str):
    global _model
    if _model is not None:
        return

    import torch
    from transformers import AutoModelForCausalLM

    # Patch SDPA before loading the model so trust_remote_code modules use it
    _patch_sdpa_for_gqa()

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    dtype = torch.float16 if device == "mps" else torch.float32

    log.info("vlm_loading", model="vikhyatk/moondream2", revision=revision, device=device)
    _model = AutoModelForCausalLM.from_pretrained(
        "vikhyatk/moondream2",
        revision=revision,
        trust_remote_code=True,
        dtype=dtype,
        device_map={"": device},
    )
    _model.eval()
    log.info("vlm_loaded", device=device)


def describe(jpeg_data: bytes, revision: str = "2025-04-14") -> str:
    """Run Moondream scene description on a JPEG screenshot. Returns a short caption."""
    from PIL import Image

    _load_model(revision)

    img = Image.open(io.BytesIO(jpeg_data)).convert("RGB")
    result = _model.caption(img, length="normal")
    caption = result["caption"] if isinstance(result, dict) else str(result)
    return caption.strip()
