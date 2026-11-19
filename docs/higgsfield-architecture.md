# Higgsfield MCP mimarisi — çıkarım

Bu belge, Higgsfield'in uzak MCP sunucusunun (`https://mcp.higgsfield.ai/mcp`, Streamable HTTP + OAuth) **dışarıdan gözlemlenebilen** mimarisini özetler. `mcp-tools` bu desenleri izler; hangi desenin bizde nasıl karşılandığı [architecture.md](architecture.md) içindedir.

## Yöntem ve sınırlar

- Kaynak: bağlayıcının sunduğu ~190 aracın adı, açıklaması ve JSON Schema'sı; ayrıca yan etkisiz üç çağrının yanıtı (`models_explore` list / get, `get_workflow_instructions`).
- Hiçbir üretim, harcama veya yazma aracı çağrılmadı.
- Sunucunun iç uygulaması görülemez. "Arka uç ipuçları" başlığındaki maddeler araç açıklamalarından çıkarımdır.
- OAuth metadata uçları ayrıca doğrulanamadı (çalışma ortamının ağı o hosta izin vermiyordu).

## 1. Katmanlar

```
MCP istemcisi (Claude, ChatGPT, …)
   │  Streamable HTTP + OAuth
   ▼
MCP cephesi — ince katman
   • sunucu `instructions`: hangi isteğin hangi araca gideceğini anlatan yönlendirme promptu
   • ~190 araç; açıklamalar = yönlendirme + onay + maliyet + yeniden deneme politikası
   • widget'lar (MCP Apps) ve başsız ikizleri
   • veri-olarak-talimat: workflow (SKILL.md paketleri) ve preset tarifleri
   ▼
Platform çekirdeği ("fnf") — MCP, web ve chat ile birlikte ön yüzlerden yalnızca biri
   • model kataloğu • job kuyruğu • medya deposu (presigned URL) • kredi defteri
   • workspace → proje → klasör → placement • moderasyon
   ▼
Dış sağlayıcılar (OpenAI, ByteDance, Kling, Suno, ElevenLabs…) · Modal sandbox · GPU kutuları ·
Cloudflare (siteler) · TikTok / Meta OAuth bağlayıcıları
```

Cephenin ince olduğuna dair kanıtlar: araç açıklamalarında web uygulamasının servislerine atıflar ("Same as inviting from the web Share dialog", "Cinema semantics"), `origin: web | chat | mcp` alanı, başka istemciler için farklı araç adları ("workspace_list (OpenAI)").

## 2. Araç aileleri

| Aile | Araçlar (örnek) | Not |
|---|---|---|
| Katalog | `models_explore` | `action = list \| search \| get \| recommend` |
| Üretim | `generate_image`, `generate_video`, `generate_audio`, `generate_3d`, `generate_*_batch`, `estimate_*_cost` | Jenerik; modele özgü kısıtlar katalogda |
| Job ve gösterim | `jobs_wait`, `job_display`, `show_generation_by_ids`, `show_generations`, `show_result` | Widget ve başsız ikizler |
| Medya | `media_upload`, `media_confirm`, `media_import_url`, `media_upload_widget`, `show_medias` | Presigned PUT akışı |
| Hesap | `balance`, `transactions`, `show_plans_and_credits`, `get/update_preferences`, `list_workspaces`, `select_workspace` | |
| Tek amaçlı dönüşümler | `upscale_image`, `upscale_video`, `outpaint_image`, `remove_background`, `reframe`, `motion_control` | Katı şema, sabit arka uç |
| Ses | `create_voice`, `create_voice_from_confirmed_audio`, `list_voices`, `voice_change`, `separate_stems` | |
| Proje / klasör / varlık | `create_project`, `list_projects`, `create_folder`, `attach_project_asset`, `move_project_asset`, `list_project_assets`, … (~30) | Paylaşım ve yayınlama dahil |
| Workflow / preset | `get_workflow_instructions`, `get_workflow_bundle_file`, `get_presets`, `get_preset_instructions`, `execute_preset` | Talimat veri olarak dağıtılır |
| Uygulama kaydı | `apps_search`, `apps_describe`, `apps_invoke` | Üç meta-araç arkasında açık uçlu eylem kümesi |
| Ürün stüdyoları | `ads_studio_*` (15), `ai_influencer_*` (6), Marketing Studio, Katana, drama | Kendi kaynak modelleri var |
| Hesaplama | `gpu_*` (19), `sandbox_exec`, `scene_builder_3d_*` (16) | Teklif → başlat → durum → iptal |
| Site / sosyal | `create_website`…, `tiktok_*`, `meta_ads_*` | OAuth bağlayıcıları, oku/yaz ayrımı |

## 3. Çekirdek akışlar

### 3.1 Model seçimi

`models_explore` yanıt öğesi (canlı çağrıdan):

```json
{"id":"seedance_2_5","name":"Seedance 2.5","provider_name":"Bytedance","output_type":"video",
 "parameters":[{"name":"duration","required":"optional","type":"number","min":4,"max":30,"default":5},
               {"name":"resolution","type":"string","options":["480p","720p","1080p","4k"],"default":"720p"}],
 "medias":[{"name":"medias","type":"image","roles":["start_image","end_image","image_references"]}],
 "aspect_ratios":["auto","21:9","16:9","4:3","1:1","3:4","9:16"],"tags":["text-to-video","reference"]}
```

Liste yanıtı `items`, `has_more`, `next_page_token` taşır. Araçlar jenerik kalır; her modelin parametreleri, medya rolleri ve en-boy oranları **veridir**.

### 3.2 Üretim

```
models_explore ──► estimate_*_cost ──► generate_* ──► jobs_wait ──► show_generation_by_ids / job_display
```

- `generate_*(params, context)`: `params = {model*, prompt, aspect_ratio, count 1–4, duration, medias[{role, value}], folder_id, …modele özel alanlar}`. `params` nesne **veya** JSON dizgesi olabilir; `additionalProperties` açıktır. `context` kısa bir analitik cümlesidir.
- Yanıt job kimlikleri döner. Sunucunun yaptığı düzeltmeler (`count` sınırlama, süreyi en yakın değere çekme, rol düzeltme) `adjustments` içinde bildirilir. Hata yanıtı `recovery_tool` taşıyabilir.
- Kullanıcı kararı gereken durumlar job açmadan **veri olarak** döner (ör. `unlim_choice`).
- `generate_*_batch(requests[{index, params}] 1–12)`: öğe başına bir job, çağıranın `index`'i korunur, atomik değildir.
- `jobs_wait(jobs[{index, job_id}] 1–12, timeout_seconds 0–15)`: uzun yoklama; `all_terminal` ve `poll_after_seconds` döner. Terminal durumlar: tamamlandı, başarısız, `nsfw`, `ip_detected`.
- "On a transport timeout the submission outcome may be unknown: do not automatically resubmit."

### 3.3 Medya

- `medias[].value` bir media_id veya önceki bir job_id'dir; **URL kabul edilmez**.
- Giriş yolları: `media_upload` (presigned PUT URL'leri, 20'ye kadar dosya) → istemci HTTP PUT → `media_confirm`; `media_import_url` (sunucu indirir, ≤50 MB); `media_upload_widget` (istemci dosyayı okuyamıyorsa UI yedeği).

### 3.4 Maliyet ve bütçe

- `estimate_*_cost` üretimle aynı `params`'ı alır; "A quote does not validate that the request is ready to generate."
- Bütçe tavanı bir parametredir: `max_credits` (AI Influencer, GPU), `expected_credits` (Ads Studio). Sunucu göndermeden önce yeniden fiyatlar ve sapmada reddeder.
- Pahalı işler için tam desen: `gpu_job_quote` → `gpu_job_start(job_id, quote_id, max_credits)` → `gpu_job_status` → `gpu_job_cancel`. `quote_id` içerik-adreslidir (64 hex); taslak 24 saat sonra düşer.
- `balance`, `transactions` (spend / refund / grant / deduct).

### 3.5 Proje, klasör, yerleşim

- Üretim `folder_id` alır; proje kimliği almaz (klasör projeyi belirtir).
- Kaynak (job / medya) bir kez saklanır; klasörler **placement** tutar. Kopyalama aynı kaynağın ikinci yerleşimini üretir; silme yalnızca yerleşimi kaldırır.
- Fiil başına bir araç; opak imleç + `has_more`; "empty page with has_more=true is not completion".
- Her çağrıda açık `workspace_id`; ayrıca kalıcı seçim (`select_workspace`).

### 3.6 Veri-olarak-talimat

- `get_workflow_instructions()` → `{"mode":"catalog","workflows":[{name, description, version}]}`; adla çağrılınca o workflow'un `SKILL.md`'si ve okunabilir dosya listesi.
- `get_workflow_bundle_file(workflow, path, include_contents)` → beyaz listeli dosya/dizin okuma.
- Workflow'lar Agent Skills biçimindedir: `SKILL.md` (ad, açıklama, sürüm) + `references/` + `scripts/`.
- Preset'ler aynı fikrin galeri hâlidir: `get_presets` (gezin), `get_preset_instructions` (tarifi çöz), `execute_preset` (çalıştır). "Browsing never authorizes execute_preset."

### 3.7 Meta-araç kaydı

- `apps_search(query, limit, cursor)` → uygulamalar ve sundukları eylemler.
- `apps_describe(app_id, action?)` → eylem başına giriş/çıkış şeması, ek açıklamalar (maliyet, yıkıcılık), yürütme kipi ve `manifest_revision`.
- `apps_invoke(app_id, action, manifest_revision, arguments)` → eski revizyon `manifest_changed` ile reddedilir. Uzun eylemler `{id, status: "queued"}` döner; durum sorgusu da bir uygulama eylemidir.
- Aynı fikir Meta Ads'te `meta_ads_tools` / `meta_ads_read` / `meta_ads_write` (kart × eylem) olarak tekrar eder; oku/yaz ayrı araçlardır.

## 4. Kesişen desenler

| # | Desen | Kanıt |
|---|---|---|
| 1 | Katalog güdümlü jenerik araçlar | `models_explore`; model kısıtları şemada değil veride |
| 2 | Geçirgen `params` zarfı | `anyOf[object, string]`, `additionalProperties` açık |
| 3 | Sunucu tarafı normalizasyon | `adjustments`, `recovery_tool` |
| 4 | Asenkron job modeli | job kimliği → `jobs_wait` uzun yoklama |
| 5 | Başsız toplu üretim | `generate_*_batch`, `index` korunur |
| 6 | Medya dolaylaması | `medias[{role, value}]`, URL yok |
| 7 | Maliyet ön-kontrolü ve bütçe kapıları | `estimate_*_cost`, `max_credits`, karar-olarak-veri |
| 8 | İdempotensi | `idempotency_key` + tekrar penceresi; doğal idempotensi (`attach`, `register_result`) |
| 9 | Şemada onay kapıları | `confirm_delete: const true`, iki aşamalı `confirm` |
| 10 | Kaynak / yerleşim ayrımı | placement'lar |
| 11 | Veri-olarak-talimat | workflow ve preset araçları |
| 12 | Meta-araç kaydı | `apps_*`, `meta_ads_*` |
| 13 | Widget + başsız ikiz | `create_voice` / `create_voice_from_confirmed_audio`; yalnızca widget'ın çağırdığı `tiktok_publish` |
| 14 | Açıklama = politika | Her açıklamada yönlendirme, onay, maliyet ve tekrar kuralı |
| 15 | Katı tek amaçlı araçlar | `upscale_image`: `additionalProperties:false`, tipli `*_id` alanları |

Belirsiz sonuçlar için evrensel kural: **durumu oku, değiştiren çağrıyı yineleme** ("Inspect assets after an uncertain receipt; never blindly repeat mutations").

## 5. Arka uç ipuçları

- İç platformun adı açıklamalarda "fnf" olarak geçer (job, medya, yetkilendirme).
- `sandbox_exec` Modal üzerinde koşar; `/home/user` 24 saat kalıcıdır.
- Web siteleri Cloudflare Workers (+ D1 / R2 / KV) üzerindedir; depo itme sunucu tarafında yapılır, sandbox'a kimlik bilgisi verilmez.
- Stripe (ödeme), PostHog (`send_feedback`), TikTok Content Posting API, Meta Marketing API.
- Moderasyon iki yerde görünür: üretimde (`nsfw`, `ip_detected`) ve yayınlamada (`awaiting_confirm`).

## 6. Tutarsızlıklar (bilinçli olarak kopyalanmadı)

- En az dört sayfalama stili: sayısal `cursor`, opak `cursor` + `has_more`, `after` / `next_page_token`, `offset` / `limit`.
- En az beş medya referans şekli: `{role, value}`, `{role, data:{id, type}}`, `{id, url, type}`, `{role, id}`, çıplak URL.
- Üç ayrı tahmin aracı ve üç ayrı toplu üretim aracı.
- Çok uzun araç açıklamaları (bazıları binlerce karakter).
- Ürüne özgü faturalama dalları (`use_unlim`, `use_free_gens`, Stripe bağlantıları).

## 7. Bizde alınan ve alınmayan

| Alındı | Alınmadı (v1) |
|---|---|
| Katalog, üretim, toplu üretim, tahmin, job bekleme ve gösterim | Ürün stüdyoları (Ads, AI Influencer, Marketing, Katana) |
| Medya yükleme / içe aktarma | Ses klonlama, 3D |
| Bakiye, defter, tercihler | Planlar, workspace seçimi, paylaşım, yayınlama |
| Proje / klasör / yerleşim (çekirdek alt küme) | GPU kiralama, sandbox |
| Workflow paketleri | Preset galerileri |
| Uygulama kaydı (üç meta-araç) | Web sitesi ve sosyal medya entegrasyonları |
| Widget'lar (üretim, galeri, yükleme) | |
