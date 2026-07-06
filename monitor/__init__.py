"""Autonomous health monitor ("doctor").

Watches the whole system on a schedule, AUTO-FIXES the safe/known classes of
runtime problems, and emails a precise diagnosis for anything that needs a human.
See monitor.doctor for the full safety boundary.
"""
from monitor.doctor import format_report, run_healthcheck

__all__ = ["run_healthcheck", "format_report"]
