"""Start the local stack and fail unless every service reports healthy."""

import sys

from compose import compose

if __name__ == "__main__":
    code = compose("up", "-d", "--build", "--wait", "--wait-timeout", "300")
    if code == 127:
        sys.exit(code)
    compose("ps")
    if code != 0:
        print("make up FAILED: at least one service did not become healthy. Run make logs.")
    else:
        print("make up OK: all services healthy.")
    sys.exit(code)
