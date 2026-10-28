"""
The Scout Agent
---------------
Role:     Business Prospect Hunter
Goal:     Discover hyper-targeted leads matching the configured niche.
Backstory: An elite intelligence operative who specialises in finding exactly
           the right people at the right companies — no noise, only signal.

Tools:    LeadScraperTool (Apollo / Hunter.io / Mock CSV)

LLM Note: CrewAI 1.x uses LiteLLM under the hood.
          Phase 1 uses "ollama/llama3" — Ollama will be installed in Phase 2.
          For now the pipeline structure and tool calls are verified.
"""

import os
from crewai import Agent
from tools.lead_scraper import LeadScraperTool
from llm_config import llm_manager
from loguru import logger


def build_scout() -> Agent:
    llm  = _get_llm()
    tool = LeadScraperTool()

    logger.info("Building Scout agent")

    return Agent(
        role="Business Prospect Hunter",
        goal=(
            "Find {limit} highly relevant business prospects in the {industry} industry "
            "with the job title '{job_title}' located in {location}. "
            "Return a clean, structured list of leads with all available contact details."
        ),
        backstory=(
            "You are an elite lead intelligence operative. You have spent years mastering "
            "the art of finding exactly the right decision-makers at high-growth companies. "
            "You only surface leads that are a perfect fit — no irrelevant noise. "
            "You are methodical, precise, and your output always includes source URLs "
            "so every claim can be verified."
        ),
        tools=[tool],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=3,
    )


def _get_llm():
    """
    Returns the LLM for the Scout via the provider manager (llm_config).
    Uses LLM_PROVIDERS priority chain (openrouter → groq → huggingface → ollama)
    with timeout-based failover, so it works both locally and when cloud-hosted.
    """
    return llm_manager.get_crewai_llm()
