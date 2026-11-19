# mcp-tools

Görsel, video ve ses üreten, Python ile yazılmış bir MCP sunucusu. Mimarisi Higgsfield MCP
sunucusundan çıkarılan desenleri izler (bkz. [`docs/higgsfield-architecture.md`](docs/higgsfield-architecture.md));
birden çok sağlayıcıyı tek bir araç yüzeyinin arkasına koyar.

- **Sağlayıcılar:** Replicate, OpenAI ve anahtar gerektirmeyen `mock` (yerelde gerçek küçük dosyalar üretir).
- **Taşımalar:** stdio (Claude Code / Claude Desktop) ve Streamable HTTP (Bearer API anahtarı, imzalı dosya bağlantıları).
- **Depolama:** tek kullanıcı, yerel: SQLite + dosya deposu (`~/.mcp-tools`).
- **Yüzey:** 37 model aracı (HTTP'de 39) + yalnızca widget'ın kullandığı bir yardımcı araç.

> Durum: `mock` sağlayıcıyla uçtan uca testlidir. Replicate ve OpenAI adaptörleri resmî istemcilerin
> kaynak kodundan çıkarılan sözleşmeye göre yazıldı ve sahte HTTP ile test edildi; **gerçek anahtarla
> henüz çalıştırılmadı**. İlk kullanımdan önce `mcp-tools doctor --live` çalıştırın.

## Kurulum

Python 3.11+ ve [uv](https://docs.astral.sh/uv/) gerekir.

```bash
uv sync
```

```bash
uv run mcp-tools doctor
```

## İstemciye bağlama

Claude Code (stdio):

```bash
claude mcp add mcp-tools -- uv run --directory /tam/yol/mcp-tools mcp-tools serve
```

HTTP:

```bash
uv run mcp-tools serve --transport http --port 8000
```

Sunucu ilk açılışta bir API anahtarı üretir ve bir kez stderr'e yazar; istemci bunu
`Authorization: Bearer <anahtar>` başlığıyla gönderir:

```bash
claude mcp add --transport http mcp-tools http://127.0.0.1:8000/mcp --header "Authorization: Bearer <anahtar>"
```

Claude Desktop ve diğer istemciler için: [`docs/clients.md`](docs/clients.md).

## Sağlayıcı anahtarları

Anahtarlar ortam değişkeninden **veya** veri dizinindeki özel bir dosyadan okunur. stdio istemcileri
sunucuyu çoğu zaman boş bir ortamla başlattığı için dosya yolu daha güvenilirdir:

```bash
mkdir -p ~/.mcp-tools && chmod 700 ~/.mcp-tools
```

```bash
printf 'REPLICATE_API_TOKEN=...\nOPENAI_API_KEY=...\n' > ~/.mcp-tools/secrets.env && chmod 600 ~/.mcp-tools/secrets.env
```

Anahtarlar veritabanına, araç sonuçlarına veya günlüklere yazılmaz. Anahtarı olmayan sağlayıcının
modelleri katalogda `available: false` olarak görünür.

```bash
uv run mcp-tools doctor --live
```

`doctor --live` her katalog girdisini sağlayıcının yayınladığı model şemasıyla karşılaştırır.

## Nasıl kullanılır

Model tipik olarak şu sırayı izler:

1. `models_explore` ile model seçer (`recommend` → `get`).
2. Gerekirse `estimate_cost` ile fiyatı öğrenir.
3. `generate_image` / `generate_video` / `generate_audio` (ya da bağımsız işler için `generate_batch`) çağırır;
   hemen job kimlikleri döner.
4. `jobs_wait` ile sonuçları bekler; sonuçlar `media_id` ve `local_path` (stdio) veya `url` (HTTP) taşır.
5. Görseli kendisi görmek için `media_get(include_preview=true)` çağırır.

Referans dosyalar her zaman **kimlikle** verilir (`med_…` veya biten bir işin `job_…` kimliği), asla URL ile.
Bağlantı ya da yerel dosya önce içe aktarılır: `media_import_url`, `media_import_path` (stdio; varsayılan
olarak yalnızca `~/.mcp-tools/inbox`), HTTP'de `media_upload` + `media_confirm`.

Diğer aileler: projeler ve klasörler (`projects_*`, `folders_*`, `assets_*`), hazır yöntemler
(`workflow_instructions`), ek küçük uygulamalar (`apps_search` → `apps_describe` → `apps_invoke`),
tek amaçlı dönüşümler (`upscale_image`, `remove_background`), hesap (`balance`, `transactions`, `preferences_*`).

## Bütçe ve güvenlik

Krediler bütçeleme için **tahmindir** (1 kredi = 0,001 USD); asıl fatura sağlayıcınınkidir.

| Ayar | Varsayılan | Anlamı |
|---|---|---|
| `MCP_TOOLS_AUTO_APPROVE_CREDITS` | 500 | Tahmini bunu aşan çağrı açık bir `max_credits` ister; yoksa hiçbir şey oluşturulmaz (`approval_required`). |
| `MCP_TOOLS_MAX_CREDITS_PER_CALL` | 5000 | Tek çağrının kesin tavanı. |
| `MCP_TOOLS_DAILY_CAP_CREDITS` | 10000 | Günlük (UTC) kesin tavan. |
| `MCP_TOOLS_INITIAL_CREDITS` | 10000 | Yeni veri dizininin açılış bakiyesi. |

Tavanlar yalnızca yapılandırmadan değişir; hiçbir araç onları yükseltemez. Kredi yalnızca komut satırından eklenir:

```bash
uv run mcp-tools credits grant 5000 --note "aylık"
```

`max_credits` ve `confirm_delete` gibi alanlar modelin niyetini belirtir, kullanıcı rızasının kanıtı
değildir; gerçek sınır yukarıdaki tavanlardır. Başarısız, reddedilen (içerik politikası) ve iptal edilen
işler otomatik iade edilir. Bir iş sağlayıcıya en fazla bir kez gönderilir.

Tüm ayarlar ve HTTP güvenlik modeli: [`docs/architecture.md`](docs/architecture.md), [`.env.example`](.env.example).

## Komutlar

| Komut | İşlev |
|---|---|
| `mcp-tools serve [--transport stdio\|http]` | Sunucuyu çalıştırır. |
| `mcp-tools worker` | İstemci bağlı değilken işleri ilerletir (uzun video işleri için). |
| `mcp-tools doctor [--live]` | Kurulumu ve sağlayıcıları denetler. |
| `mcp-tools credits balance` / `grant N` | Bakiyeyi gösterir / kredi ekler. |
| `mcp-tools catalog validate` | Katalog dosyalarını doğrular. |
| `mcp-tools gc [--dry-run]` | Süresi dolmuş yüklemeleri ve sahipsiz dosyaları temizler. |
| `mcp-tools key rotate` | HTTP API anahtarını yeniler. |

## Geliştirme

```bash
uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q
```

Gerçek bir sunucu süreciyle uçtan uca duman testi (anahtar gerekmez):

```bash
uv run python scripts/smoke.py --transport stdio
```

```bash
uv run python scripts/smoke.py --transport http
```

Widget testi Node.js ve Playwright (Chromium) bulursa çalışır, bulamazsa atlanır.

## Bilinen sınırlar

- Tek kullanıcı, tek workspace; OAuth yok (HTTP'de statik Bearer anahtarı).
- Replicate / OpenAI çağrıları gerçek anahtarla doğrulanmadı; katalogdaki model adları ve fiyatlar doğrulanmamış tahminlerdir.
- Video klipleri birleştirilmez; yükleme widget'ı ve MCP prompt kayıtları yoktur.
- Arayüzleri hazır olsa da S3 ve Postgres uygulamaları yoktur.
