# KubeProof frontend

The local evidence explorer is a React + TypeScript application built with Vite.
It reads the existing history JSON API; it does not execute Helm or Kubernetes
experiments in the browser.

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
