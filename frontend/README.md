# KubeProof frontend

The local evidence explorer is a React + TypeScript application built with Vite.
The browser submits a Helm chart archive and confirmed CPU requirements to the
loopback backend. Helm, Docker, kind and load generation run only in the backend,
after static preflight and a separate explicit approval in the UI.

For development, run the backend in one terminal and the Vite server in another:

```bash
uv run kubeproof serve --data-dir ./.kubeproof
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to the backend at
`127.0.0.1:8000`. For a single-port local setup, run `npm run build` in this
folder and open `http://127.0.0.1:8000` instead. `kubeproof serve` serves the
built files from `frontend/dist`; an installation outside the source checkout
can point to a build with `--frontend-dir PATH`. Without a build, `/` returns an
actionable error while the API remains available. The build directory is
generated and not committed.

Run `npm test` for frontend tests; `npm run build` also typechecks.

For the first real CPU experiment, install the Python `ai` extra even if you use
the deterministic pilot, package `examples/charts/cpu-fixture` with Helm, and
follow the [live CPU UI walkthrough](../docs/experiments/live-cpu-ui.md). Only
the reviewed CPU fixture contract can receive load in this version; other
uploaded charts may be inspected statically but cannot be run from this UI.
