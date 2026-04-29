from __future__ import annotations

import argparse

from .ui import FlowWorkerApp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config-name", default="")
    parser.add_argument("--slot-file", default="")
    args = parser.parse_args()
    app = FlowWorkerApp(
        config_name=str(args.config_name or "").strip() or None,
        slot_file=str(args.slot_file or "").strip() or None,
    )
    app.root.mainloop()


if __name__ == "__main__":
    main()
