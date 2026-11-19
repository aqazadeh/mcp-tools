# mcp-tools

Higgsfield MCP mimarisini izleyen, Python ile yazılmış üretken medya (görsel / video / ses) MCP sunucusu.

> Durum: geliştirme aşamasında. Mimari ve yol haritası için `docs/` klasörüne bakın.

## Hızlı başlangıç

```bash
uv sync
```

```bash
uv run mcp-tools serve
```

Sunucu varsayılan olarak stdio üzerinden konuşur ve API anahtarı gerektirmeyen `mock` sağlayıcıyla çalışır.

## Geliştirme

```bash
uv run ruff check . && uv run pyright && uv run pytest -q
```
