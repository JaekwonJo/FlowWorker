from __future__ import annotations

from .ui import FlowWorkerApp


def main() -> None:
    app = FlowWorkerApp()
    app.root.mainloop()


if __name__ == "__main__":
    main()
