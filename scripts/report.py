#!/usr/bin/env python3
"""Entry point for the optional GitLab and EOM workflows."""

import sys

from reporting.cli import main


if __name__ == "__main__":
    sys.exit(main())
