#!/bin/bash
# Kept for anything that still calls the 3:30-only helper by name (#1513);
# the logic now lives in prefetch_slot.sh, which covers every live slot.
exec bash "$(dirname "$0")/prefetch_slot.sh" 1530
