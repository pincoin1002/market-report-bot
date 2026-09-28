"""Vercel Cron entrypoint for the 12:55 UTC (EDT) candidate slot."""

from scheduler.us_open_dispatch import make_handler

handler = make_handler("edt")
