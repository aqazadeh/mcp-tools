# İstemci bağlama

## Claude Code

Depo içinden çalışırken kökteki `.mcp.json` yeterlidir. Başka bir dizinden:

```bash
claude mcp add mcp-tools -- uv run --directory /tam/yol/mcp-tools mcp-tools serve
```

Sağlayıcı anahtarlarını `~/.mcp-tools/secrets.env` dosyasına koyun (bkz. README) ya da `--env` ile verin:

```bash
claude mcp add mcp-tools --env OPENAI_API_KEY=... -- uv run --directory /tam/yol/mcp-tools mcp-tools serve
```

## Claude Desktop

`claude_desktop_config.json` içine:

```json
{
  "mcpServers": {
    "mcp-tools": {
      "command": "uv",
      "args": ["run", "--directory", "/tam/yol/mcp-tools", "mcp-tools", "serve"]
    }
  }
}
```

Claude Desktop sunucuyu asgari bir ortamla başlatır: anahtarlar için `secrets.env` dosyasını ya da bu
girdiye bir `"env": { ... }` bloğu kullanın.

## HTTP

```bash
uv run mcp-tools serve --transport http --host 127.0.0.1 --port 8000
```

- İlk açılışta üretilen API anahtarı bir kez stderr'e yazılır; diskte yalnızca SHA-256 özeti durur.
  Yenilemek için `mcp-tools key rotate`.
- `--no-auth` yalnızca loopback'e bağlıyken ve ek host / genel URL tanımlı değilken kabul edilir. Loopback kullanıcı
  sınırı değildir: bu kipte makinedeki her kullanıcı ve süreç bütün araçları çağırabilir.
- Ek host adı (`MCP_TOOLS_HTTP_ALLOWED_HOSTS`) ya da genel URL tanımlıysa sunucu "yerel değil" sayılır:
  `media_import_path` ve `local_path` kapanır.
- Loopback dışına bağlamak için TLS (`--tls-cert`, `--tls-key`) ya da önde TLS sonlandıran bir vekil ve
  `--insecure-http` gerekir. Dışarıdan erişilen adresi `MCP_TOOLS_PUBLIC_BASE_URL` ile verin: imzalı dosya
  bağlantıları isteğin `Host` başlığından değil bu adresten kurulur.
- claude.ai web bağlayıcıları OAuth bekler; statik Bearer başlığı kabul etmeyebilir. Bu sürüm OAuth sunmaz.

Uç noktalar: `POST/GET/DELETE /mcp` (anahtar gerekir), `GET /files/{media_id}` ve `PUT /upload/{media_id}`
(imzalı, en fazla 15 dakikalık bağlantılar), `GET /healthz`. Geri kalan her yol 404 döner.

## Uzun işler

stdio'da her istemci kendi sunucu sürecini başlatır ve kapanınca süreci sonlandırır. Yarım kalan işleri
bir sonraki süreç devralır, ancak sağlayıcı çıktıları kısa sürede silinebilir (Replicate ~1 saat). Uzun
video işleri için HTTP kipini ya da ayrı bir `mcp-tools worker` sürecini kullanın.

## Widget'lar

MCP Apps destekleyen istemcilerde `generate_*`, `show_jobs` ve `show_generations` sonuçları, işler
bittikçe güncellenen bir görüntüleyiciyle gösterilir. Desteklemeyen istemcilerde aynı sonuçlar metin
olarak eksiksizdir. Kapatmak için `MCP_TOOLS_UI_ENABLED=false`.
