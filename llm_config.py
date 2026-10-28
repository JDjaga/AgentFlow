"""
LLM Provider Manager — OpenRouter primary with timeout-based fallback
=====================================================================
Makes the whole project CLOUD-HOSTABLE by removing the hard dependency on
local Ollama. Every LLM call can now go through a ranked list of providers
and automatically fail over when one is slow, down, or unconfigured.

Supported providers (all OpenAI-compatible /chat/completions):
  openrouter  — https://openrouter.ai/api/v1       (OPENROUTER_API_KEY)
  groq        — https://api.groq.com/openai/v1     (GROQ_API_KEY)      ← fastest free
  huggingface — https://router.huggingface.co/v1   (HUGGINGFACEHUB_API_TOKEN)
  ollama      — http://localhost:11434/v1          (no key)            ← local/offline

Configure the priority chain in .env:
  LLM_PROVIDERS=openrouter,groq,huggingface,ollama
  LLM_TIMEOUT=25          # per-call timeout (seconds) before we fail over

Usage:
  from llm_config import llm_manager

  # For raw completions (security checks use this):
  text = llm_manager.complete("Say hi", system="You are terse.", max_tokens=32)

  # For CrewAI agents:
  llm = llm_manager.get_crewai_llm()   # returns crewai.LLM (or model string fallback)

First call probes providers in order and caches the fastest healthy one, so
startup cost is a single tiny request. Subsequent calls use the cached winner
and only fail over on error/timeout.
"""

import os
import time
import httpx
from dataclasses import dataclass, field
from typing import Optional, List
from dotenv import load_dotenv
from loguru import logger

# Load .env at import time so PROVIDER_SPECS (module-level os.getenv calls) and
# the LLMManager provider chain see configured values no matter which module
# imports us first (server.py / crew.py import order shouldn't matter).
load_dotenv()


# ─── Provider Registry ────────────────────────────────────────────────────────
# How each provider maps to an OpenAI-compatible endpoint + its CrewAI/LiteLLM
# model string + the env var holding its API key.

@dataclass
class ProviderSpec:
    name:        str
    base_url:    str          # OpenAI-compatible root (…/v1)
    default_model: str        # default chat model for this provider
    api_key_env: str = ""     # env var that holds the key ("" = none needed)
    litellm_prefix: str = ""  # prefix CrewAI/LiteLLM expects (e.g. "openrouter/")


PROVIDER_SPECS = {
    "openrouter": ProviderSpec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        default_model=os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct"),
        api_key_env="OPENROUTER_API_KEY",
        litellm_prefix="openrouter",
    ),
    # ── Premium frontier models (all OpenAI-compatible endpoints) ────────────
    "anthropic": ProviderSpec(      # Claude — via Anthropic's OpenAI-compat endpoint
        name="anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
        api_key_env="ANTHROPIC_API_KEY",
        litellm_prefix="anthropic",
    ),
    "openai": ProviderSpec(         # ChatGPT — GPT-4o family
        name="openai",
        base_url="https://api.openai.com/v1",
        default_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        api_key_env="OPENAI_API_KEY",
        litellm_prefix="openai",
    ),
    "gemini": ProviderSpec(         # Google Gemini — via OpenAI-compat endpoint
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        default_model=os.getenv("GEMINI_MODEL", "gemini-1.5-flash"),
        api_key_env="GEMINI_API_KEY",
        litellm_prefix="gemini",
    ),
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        default_model=os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
        api_key_env="GROQ_API_KEY",
        litellm_prefix="groq",
    ),
    "huggingface": ProviderSpec(
        name="huggingface",
        base_url="https://router.huggingface.co/v1",
        default_model=os.getenv("HF_MODEL", "meta-llama/Llama-3.3-70B-Instruct"),
        api_key_env="HUGGINGFACEHUB_API_TOKEN",
        litellm_prefix="huggingface",
    ),
    "ollama": ProviderSpec(
        name="ollama",
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434") + "/v1",
        default_model=os.getenv("OLLAMA_MODEL", "llama3.2"),
        api_key_env="",              # local — no key required
        litellm_prefix="ollama",
    ),
}


@dataclass
class ActiveProvider:
    spec:   ProviderSpec
    model:  str
    latency: float = 0.0


@dataclass
class LLMManager:
    """
    Resolves the healthiest provider from the configured priority chain and
    exposes two surfaces:
      complete()       → raw text completion with runtime failover (httpx)
      get_crewai_llm() → a CrewAI LLM bound to the active provider
    """
    providers:      List[str] = field(default_factory=list)
    timeout:        float     = 25.0
    probe_timeout:  float     = 8.0
    _active:        Optional[ActiveProvider] = None
    _initialized:   bool      = False

    # ── Configuration ────────────────────────────────────────────────────────
    def __post_init__(self):
        if not self.providers:
            raw = os.getenv("LLM_PROVIDERS", "openrouter,groq,huggingface,ollama")
            self.providers = [p.strip().lower() for p in raw.split(",") if p.strip()]
        self.timeout = float(os.getenv("LLM_TIMEOUT", str(self.timeout)))

        # De-duplicate while preserving order
        seen = set()
        self.providers = [p for p in self.providers if not (p in seen or seen.add(p))]

    def _known(self, name: str) -> bool:
        return name in PROVIDER_SPECS

    def _has_key(self, spec: ProviderSpec) -> bool:
        return (not spec.api_key_env) or bool(os.getenv(spec.api_key_env))

    # ── Provider probing (the "is it fast enough?" check) ────────────────────
    def _probe(self, spec: ProviderSpec, model: str) -> Optional[float]:
        """
        Sends a 1-token ping. Returns latency in seconds if healthy, else None.
        This is what lets us fail over when a provider 'takes too much time'.
        """
        if not self._has_key(spec):
            return None
        try:
            t0 = time.time()
            r = httpx.post(
                f"{spec.base_url}/chat/completions",
                headers=self._headers(spec),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "temperature": 0,
                },
                timeout=self.probe_timeout,
            )
            r.raise_for_status()
            return time.time() - t0
        except Exception as e:
            logger.debug(f"Probe failed for {spec.name}: {e}")
            return None

    def _headers(self, spec: ProviderSpec) -> dict:
        headers = {"Content-Type": "application/json"}
        if spec.api_key_env:
            headers["Authorization"] = f"Bearer {os.getenv(spec.api_key_env, '')}"
        # OpenRouter likes these for attribution (harmless elsewhere)
        if spec.name == "openrouter":
            headers.setdefault("HTTP-Referer", os.getenv("APP_URL", "http://localhost:8001"))
            headers.setdefault("X-Title", "AgentFlow")
        return headers

    # ── Resolution: pick first healthy provider in priority order ────────────
    def resolve(self, force: bool = False) -> Optional[ActiveProvider]:
        if self._active and not force:
            return self._active

        for name in self.providers:
            if not self._known(name):
                logger.warning(f"Unknown LLM provider '{name}' in LLM_PROVIDERS — skipping.")
                continue
            spec = PROVIDER_SPECS[name]
            model = spec.default_model

            latency = self._probe(spec, model)
            if latency is not None:
                self._active = ActiveProvider(spec=spec, model=model, latency=latency)
                self._initialized = True
                logger.success(
                    f"LLM provider resolved → {name} ({model}) in {latency*1000:.0f}ms"
                )
                return self._active
            else:
                reason = (
                    f"missing {spec.api_key_env}" if spec.api_key_env and not self._has_key(spec)
                    else "unreachable/too slow"
                )
                logger.warning(f"LLM provider {name} unavailable ({reason}) — trying next.")

        logger.error("No LLM provider available. Check LLM_PROVIDERS + API keys.")
        return None

    # ── Runtime call with failover ───────────────────────────────────────────
    def complete(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 700,
        temperature: float = 0.2,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Chat completion with automatic failover across the whole provider chain.
        Returns the assistant text. Raises only if EVERY provider fails.
        """
        active = self.resolve()
        if active is None:
            raise RuntimeError(
                "No LLM provider available. Set LLM_PROVIDERS and the matching API keys."
            )

        # Build the try-order: active first, then the rest as failovers
        order = [active.spec.name] + [p for p in self.providers if p != active.spec.name]

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_err: Optional[Exception] = None
        for name in order:
            if not self._known(name):
                continue
            spec = PROVIDER_SPECS[name]
            if not self._has_key(spec):
                continue
            model = active.model if name == active.spec.name else spec.default_model
            try:
                r = httpx.post(
                    f"{spec.base_url}/chat/completions",
                    headers=self._headers(spec),
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                    },
                    timeout=timeout or self.timeout,
                )
                r.raise_for_status()
                data = r.json()
                text = data["choices"][0]["message"]["content"].strip()

                # If we drifted off the cached winner, re-cache the survivor
                if self._active is None or self._active.spec.name != name:
                    self._active = ActiveProvider(spec=spec, model=model)
                    logger.warning(f"LLM failover engaged → now using {name} ({model})")
                return text

            except Exception as e:
                logger.warning(f"{name} call failed/timed out ({e}) — failing over.")
                last_err = e
                continue

        raise RuntimeError(f"All LLM providers failed. Last error: {last_err}")

    # ── CrewAI surface ───────────────────────────────────────────────────────
    def crewai_model(self) -> str:
        """LiteLLM-style model string for the active provider (e.g. 'groq/llama-3.3-70b-versatile')."""
        active = self.resolve()
        if active is None:
            # Last resort: return ollama string so offline dev still constructs agents
            return f"ollama/{PROVIDER_SPECS['ollama'].default_model}"
        spec = active.spec
        return f"{spec.litellm_prefix}/{active.model}"

    def get_crewai_llm(self) -> str:
        """
        Returns a LiteLLM model string for the active provider, to pass to
        crewai.Agent(llm=...). We deliberately return a plain string (not a
        crewai.LLM object) — passing timeout kwargs to crewai.LLM leaks them
        into the provider call and raises TypeError. Timeout/failover for raw
        completions is handled by complete() above, not by CrewAI.
        """
        return self.crewai_model()


# ─── Singleton ────────────────────────────────────────────────────────────────
llm_manager = LLMManager()
