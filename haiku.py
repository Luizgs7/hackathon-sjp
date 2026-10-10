"""Claude Messages API: apenas texto público da resposta, sem blocos de thinking."""
import logging
import os

import httpx


class HaikuError(RuntimeError):
    pass


def generate(messages, max_tokens=1200, *, effort=None, transport=None):
    key = os.getenv("ANTHROPIC_API_KEY", "")
    level = effort or os.getenv("HAIKU_EFFORT", "medium")
    if not key:
        raise HaikuError("Claude sem chave")
    if level not in {"low", "medium", "high", "xhigh", "max"}:
        raise HaikuError("Effort inválido")
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    conversation = [{"role": m["role"], "content": m["content"]}
                    for m in messages if m["role"] in ("user", "assistant")]
    body = {"model": os.getenv("HAIKU_MODEL", "claude-haiku-5-5"),
            "max_tokens": max_tokens, "messages": conversation,
            "output_config": {"effort": level}}
    # Níveis mais altos exigem adaptive; low/medium/high também aceitam disabled.
    body["thinking"] = {"type": "disabled" if level in {"low", "medium", "high"} else "adaptive"}
    if system:
        body["system"] = system
    try:
        with httpx.Client(timeout=float(os.getenv("HAIKU_TIMEOUT", "30")), transport=transport) as client:
            response = client.post("https://api.anthropic.com/v1/messages", json=body,
                                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            if response.status_code != 200:
                raise HaikuError(f"Claude HTTP {response.status_code}")
            data = response.json()
        if data["stop_reason"] != "end_turn":
            raise HaikuError("Claude retornou resposta incompleta ou recusada")
        text = "\n".join(b["text"] for b in data["content"] if b["type"] == "text").strip()
        if not text:
            raise HaikuError("Claude retornou vazio")
        usage = data.get("usage", {})
        logging.getLogger("missoes").info("Claude: entrada=%s saída=%s effort=%s",
                                         usage.get("input_tokens"), usage.get("output_tokens"), level)
        return text
    except HaikuError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HaikuError("Claude indisponível ou resposta inválida") from None
