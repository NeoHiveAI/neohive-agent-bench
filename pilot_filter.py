#!/usr/bin/env python3
"""Emit an anchored regex matching EXACTLY the pinned pilot instance IDs.

Used as mini-swe-agent's `--filter` so a run targets only the HIVE-288 subset:
    mini-extra swebench --subset verified --split test \
        --filter "$(python3 pilot_filter.py)" ...
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "pilot_subset.json")) as f:
    ids = [i["instance_id"] for i in json.load(f)["instances"]]
print("^(" + "|".join(re.escape(i) for i in ids) + ")$")
