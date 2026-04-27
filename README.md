# Flow Worker

Flow Worker is a standalone desktop app for Flow image/video automation.

Current status:

- standalone repo and standalone runtime structure
- Grok Worker style UI
- local config, prompt file, project profile management
- independent Edge launcher/attach manager
- prompt plan builder for image/video modes
- one-time legacy data import from `Flow Classic Plus` prompt/config files

This repo intentionally does not import or execute `Flow Classic Plus` code.

## Run

Windows:

- `FlowWorker_실행.vbs`
- or `python -m flow_worker.main`

## Layout

- `flow_worker/config.py`
- `flow_worker/prompt_parser.py`
- `flow_worker/browser.py`
- `flow_worker/automation.py`
- `flow_worker/ui.py`
