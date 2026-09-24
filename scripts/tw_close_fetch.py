#!/usr/bin/env python3
"""TW-close entry point with market-aware cross-market quote policy."""

from __future__ import annotations

import fetch_market_data
from tw_close_policy import fetch_tw_close_observations


# Patch only the TW-close executable boundary.  The shared fetcher remains the
# canonical implementation for snapshot construction and validation.
fetch_market_data.fetch_session_observations = fetch_tw_close_observations


if __name__ == "__main__":
    fetch_market_data.main()
