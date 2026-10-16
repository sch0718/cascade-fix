import os
import sys

if "graphql" in sys.argv[1:]:
    with open(os.environ["CASCADE_FAKE_GH_FILE"], encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)
sys.exit(1)
