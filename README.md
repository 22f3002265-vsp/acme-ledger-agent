# Acme Appliances Ledger Agent — Vercel deployment

## Project structure

```text
acme-ledger-agent-vercel/
├── main.py              # FastAPI app; exports `app`
├── requirements.txt     # Python dependencies
├── export.json          # Order ledger snapshot used by the app
├── rates.json           # USD conversion rates
├── vercel.json          # Vercel configuration schema
├── .python-version      # Python runtime version
├── .gitignore
└── README.md
```

Vercel detects the FastAPI app from the root-level `app.py`, a documented FastAPI entrypoint; no custom rewrite is needed. The deployed endpoint is `/answer`.

## Deploy with GitHub

1. Create a GitHub repository and upload all files in this folder to its root.
2. In Vercel, select **Add New → Project**, import the repository, and deploy.
3. Keep the Root Directory set to `./`. Vercel should detect the Python/FastAPI app automatically; leave Build and Output settings at their defaults.
4. After deployment, test `https://YOUR-PROJECT.vercel.app/` and `https://YOUR-PROJECT.vercel.app/docs`.

## Deploy with Vercel CLI

```bash
npm install -g vercel
cd acme-ledger-agent-vercel
vercel
vercel --prod
```

## Test the endpoint

```bash
curl -X POST 'https://YOUR-PROJECT.vercel.app/answer' \
  -H 'Content-Type: application/json' \
  -d '{"question":"What was the total revenue in USD from the North region in March 2026?"}'
```

The response has the shape `{"answer": 1099.7}` for the bundled sample data.

## Important data note

`export.json` is the 100-row snapshot currently bundled with this project. If the assignment expects the full live export, replace it with the complete export before deployment.


## Redeploying after a 405 error

Make sure the Vercel Root Directory contains `app.py`, `requirements.txt`, `export.json`, and `rates.json` directly (not nested one folder deeper). Redeploy after pushing these files. Test `POST /answer` rather than opening `/answer` in a browser, since browsers navigate with `GET`.
\n\n## Fix for product revenue ranking\n\nThe revenue-ranking parser recognizes questions such as “top-selling product by revenue” and returns the product name, rather than the overall revenue total.\n