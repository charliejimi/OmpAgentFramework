"""Starter Domain Pack: Service & Infrastructure Reliability Monitoring Example.

Demonstrates how any engineering team can plug their own domain data, Host Tools,
deterministic fact-checker, rule simulator, and offline planners into `omp_framework`.

To adapt this blueprint for your own use case (e.g., FinOps, SecOps, Manufacturing,
Healthcare, or Environmental Monitoring):
1. Replace or extend the sample data and Host Tools in `src/starter_domain/pack.py`.
2. Edit the agent markdown profiles in `agents/omp/*.md`.
3. Keep `src/omp_framework/` untouched!
"""

from starter_domain.pack import StarterDomainPack, build_starter_pack

__all__ = ["StarterDomainPack", "build_starter_pack"]
