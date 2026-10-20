# Komtaş Asset Portföyü

SAT ofisinin asset portföyü uygulaması: 360 dağılımı, asset kataloğu, aday değerlendirme (15 soru / eleme kapıları), asset içerikleri (PDF önizleme ve indirme), fırsat takibi ve teklif üretimi.

Uygulama ilk olarak bir Claude artifact'i olarak geliştirildi. Bu repo, aynı arayüzü ekibin kendi altyapısında çalışan bağımsız bir web uygulamasına dönüştürür:

- **Ön yüz:** `web/index.html`. Artifact'ten birebir alınmıştır. Tek fark başa eklenen `claude-shim.js` satırıdır.
- **Shim:** `web/claude-shim.js`. Artifact çalışma zamanının (`claude.use("db" | "user" | "downloads")`) yerini tutar ve onu backend'e bağlar.
- **Backend:** `backend/app` (FastAPI + PostgreSQL). Genel bir collection/doc deposudur. Değişiklikler PostgreSQL `LISTEN/NOTIFY` ve SSE ile anında tüm açık sekmelere yayılır, birden çok replica ile de çalışır. Her yazma `document_history` tablosuna da kaydedilir.
- **İçerikler:** `web/docs/<asset>/*.pdf|*.jpg`. Paket dokümanları ve önizlemeleridir.
- **Başlangıç verisi:** `seed/*.json`. Artifact veritabanından 2026-10-20 tarihinde alınmıştır: 24 asset, 38 aday, 2 fırsat. Veritabanı boşken ilk açılışta otomatik yüklenir.

> ⚠️ Repo müşteri adları, fırsat notları ve fiyatlandırma şablonları içerir. **Private** tutulmalıdır.

## Yerelde çalıştırma

```bash
docker compose up --build
# http://localhost:8080
```

Varsayılan `AUTH_MODE=none` ile herkes yazabilir. Bu mod yalnızca geliştirme içindir.

Docker olmadan çalıştırma:

```bash
pip install -r backend/requirements.txt
cd backend && DATABASE_URL=postgresql://user:pass@localhost:5432/portfoy \
  WEB_DIR=../web SEED_DIR=../seed uvicorn app.main:app --port 8080
```

## Yapılandırma

| Değişken | Varsayılan | Açıklama |
|---|---|---|
| `DATABASE_URL` | `postgresql://portfoy:portfoy@localhost:5432/portfoy` | PostgreSQL bağlantısı. Şema açılışta otomatik kurulur. |
| `AUTH_MODE` | `none` | `none` geliştirme içindir. `header`: önde oauth2-proxy (Entra ID / Google) çalışır. `basic`: `BASIC_USERS` listesindeki kullanıcılar giriş yapar. |
| `AUTH_HEADER` | `X-Auth-Request-Email` | `header` modunda kullanıcıyı taşıyan başlık. |
| `WRITERS` | `*` | Yazma yetkisi olanlar: `*`, e-posta listesi ve/veya `@komtas.com.tr` gibi alan adları. Diğer kullanıcılar salt okunur görür. |
| `BASIC_USERS` | – | `ad:parola,ad2:parola2`. Secret üzerinden verilmelidir. |
| `SEED_ON_EMPTY` | `true` | Veritabanı boşsa `seed/` klasörünü yükler. |

`header` modunda kimlik doğrulamasız istekler (PDF'ler dahil) 401 alır. Uygulamaya yalnızca kimlik vekili üzerinden erişilebilmelidir.

## Kubernetes

```bash
cp k8s/secret.example.yaml k8s/secret.yaml   # değerleri doldurun, repoya koymayın
kubectl apply -f k8s/namespace.yaml
kubectl -n asset-portfoyu apply -f k8s/secret.yaml
kubectl apply -k k8s/
```

- `k8s/postgres.yaml` tek replica bir PostgreSQL StatefulSet kurar (5 Gi PVC). Kurum içinde zaten bir PostgreSQL varsa (ör. Corporate Memory'nin kullandığı), bu dosyayı `kustomization.yaml`'dan çıkarıp `DATABASE_URL`'i o sunucuya yönlendirmek yeterlidir.
- `k8s/ingress.yaml` ingress-nginx ve oauth2-proxy varsayar. Alan adlarını ve oauth2-proxy adresini kendi ortamınıza göre değiştirin. SSE için proxy buffering kapatılmıştır.
- İmaj `main`'e her push'ta GitHub Actions ile `ghcr.io/<owner>/komtas-asset-portfoyu` adresine yayınlanır (`.github/workflows/docker.yml`). Kümenin bu imajı çekebilmesi için bir `imagePullSecret` gerekebilir.

## Veri yedeği

```bash
./scripts/export_db.sh https://asset-portfoyu.example.komtas.com.tr > yedek.json
```

Çıktı `seed/` biçimindedir. Yeni bir ortama taşımak için koleksiyonlara bölüp `seed/` altına koymak yeterlidir.

## Artifact ile senkron

Asset paketleri şu an Claude tarafında da üretiliyor. Tek kaynak belirlenene kadar şu kural geçerlidir: **veri bu uygulamada, yeni paket dokümanları ve arayüz değişiklikleri artifact'ten buraya aktarılır.**

1. Artifact'in güncel `index.html`'i alınır ve shim eklenir:
   `python3 scripts/inject_shim.py artifact-index.html web/index.html`
2. Yeni veya değişen dokümanlar `web/docs/<asset>/` altına kopyalanır.
3. Commit, push. İmaj otomatik yeniden üretilir.

## API

| Yöntem | Yol | Açıklama |
|---|---|---|
| GET | `/api/me` | Oturumdaki kullanıcı ve yazma yetkisi |
| GET | `/api/collections/{c}?order_by=alan` | Koleksiyondaki tüm belgeler |
| POST | `/api/collections/{c}` | Yeni belge (id otomatik) |
| GET / PUT / DELETE | `/api/docs/{c}/{id}` | Tek belge oku / yaz (tam değiştirme) / sil |
| GET | `/api/events` | SSE değişiklik akışı (`change`: `{collection,id,op}`) |
| GET | `/api/export` | Tüm veri (yedek) |
| GET | `/healthz`, `/readyz` | Sağlık kontrolleri |
