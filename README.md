# AYKALEN API — source of the deployed service

This repository holds the complete source of the API running at `api.aykalen.com`: exactly the
files the production Docker image is built from (the `Dockerfile` here is the one used). It is
published automatically after every deploy.

The service uses Swiss Ephemeris through `pyswisseph`, licensed under the GNU Affero General Public
License, and the AYKALEN calculation engine (`vendor/astro_engine`, from
[AYKALEN/aykalen-astrology-engine](https://github.com/AYKALEN/aykalen-astrology-engine)). This
source is offered under the GNU Affero General Public License v3.0 or later — see `LICENSE`.

## Checking that this is the running version

- `SHA256SUMS` lists every file copied into the image, with its SHA-256; `sha256sum -c SHA256SUMS` checks
  them. `README.md`, `LICENSE` and `SHA256SUMS` itself are added by the publisher and are not listed.
- `sha256sum SHA256SUMS` equals `content_sha256` at `https://api.aykalen.com/source` and the hash
  in this commit's message.

## Running it

```sh
docker build -t aykalen-api .
docker run -p 127.0.0.1:8000:8000 aykalen-api
curl http://127.0.0.1:8000/health
```

Swiss Ephemeris data files are not included; the engine falls back to its built-in Moshier
ephemeris and discloses the flags it used in every response.
