from __future__ import annotations

import os
import sys

from .core import (
    PROOF_SUPERVISOR_ENV,
    internal_proof_supervisor_main,
    main,
)


if __name__ == "__main__":
    if os.environ.pop(PROOF_SUPERVISOR_ENV, "") == "1":
        raise SystemExit(internal_proof_supervisor_main(sys.argv[1:]))
    raise SystemExit(main())
