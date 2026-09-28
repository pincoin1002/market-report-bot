"""Vercel Cron entrypoint for the 13:55 UTC (EST) candidate slot."""

from scheduler.us_open_dispatch import make_handler

handler = make_handler("est")
