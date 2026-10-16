import os
import sys

if "graphql" in sys.argv[1:]:
    with open(os.environ["CASCADE_FAKE_GH_FILE"], encoding="utf-8") as f:
        sys.stdout.write(f.read())
    sys.exit(0)
if sys.argv[1:3] == ["pr", "view"] and os.environ.get("CASCADE_FAKE_GH_HEAD"):
    sys.stdout.write(os.environ["CASCADE_FAKE_GH_HEAD"] + "\n")
    sys.exit(0)
sys.exit(1)
