# Integración Odoo ⇄ FastAPI ⇄ Magento — Contrato compartido

> **Este documento es un contrato compartido entre tres repositorios.** Cualquier cambio en el formato de los payloads, endpoints o autenticación debe reflejarse aquí y comunicarse a los tres proyectos:
> - **Odoo** (`odoo.sebastianartaza.com`) — origen del contenido (source of truth del CMS).
> - **FastAPI** (`www.sebastianartaza.com`) — middleware / orquestador.
> - **Magento** (`ituc.sebastianartaza.com`) — destino (tienda de celulares).
>
> Mantener una copia idéntica de este archivo en los tres repos.

## 1. Objetivo

Gestionar el contenido de **CMS de Magento desde Odoo**. El contenido se crea/edita en Odoo, se envía al middleware FastAPI, y este aplica los cambios en Magento vía su REST API.

**Fase 1 (este documento):** CMS Pages y CMS Blocks.
**Fases futuras (fuera de alcance):** descripciones de catálogo (productos/categorías), media, traducciones por store-view.

## 2. Topología

Los tres proyectos viven en el mismo servidor de Hetzner, cada uno en su propio `docker compose`, todos detrás de la **misma red Traefik** y publicados por HTTPS:

| Servicio | URL pública | Rol |
|----------|-------------|-----|
| Odoo | `https://odoo.sebastianartaza.com` | Crea/edita contenido CMS. Hace push al middleware. |
| FastAPI | `https://www.sebastianartaza.com` | Middleware. Recibe de Odoo, transforma y escribe en Magento. |
| Magento | `https://ituc.sebastianartaza.com` | Tienda. Expone su REST API (`/rest/...`). |

**Decisión de transporte:** la comunicación entre servicios usa las **URLs públicas HTTPS vía Traefik** (no la red interna de Docker). TLS termina en Traefik.

### Flujo de alto nivel

```
┌─────────┐  push al guardar   ┌──────────┐   REST API Magento   ┌─────────┐
│  Odoo   │ ─────────────────► │ FastAPI  │ ───────────────────► │ Magento │
│         │  POST/PUT JSON     │ (middle) │  /rest/V1/cmsPage...  │         │
│         │  Bearer JWT        │          │  Bearer admin token   │         │
└─────────┘ ◄───────────────── └──────────┘ ◄─────────────────── └─────────┘
             respuesta con          almacena mapping              id Magento
             magento_id             identifier ⇄ magento_id
```

## 3. Disparador (Odoo → FastAPI)

> **Estado:** el lado Odoo ya está implementado (módulo `artaza_magento_connect`). Esta sección describe el comportamiento real con el que FastAPI debe ser compatible.

**Push inmediato al guardar.** Cuando un registro CMS se crea o actualiza en Odoo (`create` / `write`), Odoo llama de forma **síncrona** al endpoint correspondiente del middleware en el mismo momento del guardado.

- **Odoo decide el método**, no el middleware:
  - Si el registro **no** tiene `magento_id` guardado todavía → `POST` a la colección (`/cms/pages` o `/cms/blocks`).
  - Si **ya** tiene `magento_id` → `PUT` a `/cms/{recurso}/{identifier}`.
  - Aun así, FastAPI debe tratar ambos como **upsert idempotente por `identifier`** (defensivo: un `POST` con un `identifier` que ya existe en Magento debe actualizar, no duplicar).
- **Archivar = desactivar, no borrar.** Al archivar un registro en Odoo se pone `active: false` y se re-sincroniza por el flujo normal (`POST`/`PUT`). **Odoo NO llama a `DELETE`** en la Fase 1.
- Tras una respuesta correcta, Odoo guarda el `magento_id` devuelto y marca el registro como `synced`; ante un fallo lo deja en `error` con el mensaje. Hay un botón **"Sincronizar ahora"** para reintentar manualmente.
- El envío es síncrono hoy; encolarlo (cron/cola) queda como mejora futura y no cambia el contrato HTTP.

## 4. Autenticación

### 4.1 Odoo → FastAPI: OAuth2 / JWT (client credentials)

1. Odoo obtiene un token llamando a `POST https://www.sebastianartaza.com/api/v1/auth/token` con sus credenciales de cliente (`client_id` + `client_secret`, guardadas en el `.env` de Odoo).
2. FastAPI responde con un **JWT de vida corta** (`access_token`, `expires_in`).
3. Odoo incluye `Authorization: Bearer <access_token>` en cada llamada al middleware y renueva el token cuando expira (o ante un `401`).

```
POST /api/v1/auth/token
Content-Type: application/json

{ "grant_type": "client_credentials",
  "client_id": "odoo-cms",
  "client_secret": "***" }

200 OK
{ "access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600 }
```

El JWT lo firma FastAPI (HS256/RS256, secreto/clave en su `.env`) e incluye al menos: `sub` (client_id), `scope` (p.ej. `cms:write`), `exp`, `iat`.

### 4.2 FastAPI → Magento: token de integración / admin

FastAPI se autentica contra Magento con su propio mecanismo (Bearer admin token o token de integración de Magento), gestionado **internamente por el middleware**. Odoo nunca ve credenciales de Magento.

## 5. Modelo de datos y mapeo

**Clave natural:** el campo `identifier` (URL key del CMS de Magento) es la clave estable que vincula los tres sistemas. Debe ser único y no cambiar tras la creación.

- Odoo genera/mantiene el `identifier`.
- El middleware debería mantener un mapping `identifier ⇄ magento_id` (o resolverlo contra Magento por `identifier`) para garantizar el upsert idempotente, **independientemente** del método HTTP que use Odoo.
- `magento_id` es opaco para Odoo: solo lo guarda y lo reenvía en `magento_id` si lo tiene (será `null` en el primer envío).

### Tipos exactos de los campos del payload

Importante para el modelo Pydantic de FastAPI — así es como Odoo serializa cada campo:

| Campo | Tipo JSON | Notas |
|-------|-----------|-------|
| `source` | string | Siempre `"odoo"`. |
| `source_id` | integer | ID interno del registro en Odoo. |
| `identifier` | string | Clave natural, sin espacios. **Requerido.** |
| `title` | string | **Requerido.** |
| `active` | boolean | |
| `content` | string | HTML. Puede venir vacío (`""`). |
| `store_id` | array de integers | P.ej. `[0]`. `0` = vista admin/por defecto. |
| `magento_id` | integer \| null | `null` en el primer envío. |
| `content_heading` | string | Solo Pages. |
| `page_layout` | string | Solo Pages. P.ej. `"1column"`. |
| `meta_title` / `meta_keywords` / `meta_description` | string | Solo Pages. |
| `sort_order` | **string** | Solo Pages. Ojo: Odoo lo envía como **string** (p.ej. `"0"`), igual que la REST API de Magento. |

## 6. Contrato de la API del middleware (FastAPI)

Base URL: `https://www.sebastianartaza.com/api/v1`
Todas las rutas requieren `Authorization: Bearer <JWT>`.
Content-Type: `application/json`.

### 6.1 CMS Pages

| Método | Ruta | Acción |
|--------|------|--------|
| `POST` | `/cms/pages` | Crear o upsert por `identifier`. |
| `PUT`  | `/cms/pages/{identifier}` | Actualizar página existente. |
| `DELETE` | `/cms/pages/{identifier}` | Eliminar (opcional, ver §8). |
| `GET`  | `/cms/pages/{identifier}` | Leer estado actual en Magento. |

**Payload de página (Odoo → FastAPI):**

```json
{
  "source": "odoo",
  "source_id": 42,
  "identifier": "envios-y-devoluciones",
  "title": "Envíos y Devoluciones",
  "active": true,
  "content_heading": "Política de envíos",
  "content": "<p>Contenido HTML...</p>",
  "page_layout": "1column",
  "meta_title": "Envíos y devoluciones | iTUC",
  "meta_keywords": "envio, devolucion",
  "meta_description": "Conoce nuestra política...",
  "sort_order": "0",
  "store_id": [0],
  "magento_id": null
}
```

### 6.2 CMS Blocks

| Método | Ruta | Acción |
|--------|------|--------|
| `POST` | `/cms/blocks` | Crear o upsert por `identifier`. |
| `PUT`  | `/cms/blocks/{identifier}` | Actualizar bloque existente. |
| `DELETE` | `/cms/blocks/{identifier}` | Eliminar (opcional, ver §8). |
| `GET`  | `/cms/blocks/{identifier}` | Leer estado actual en Magento. |

**Payload de bloque (Odoo → FastAPI):**

```json
{
  "source": "odoo",
  "source_id": 17,
  "identifier": "banner-home-celulares",
  "title": "Banner Home Celulares",
  "active": true,
  "content": "<div>...</div>",
  "store_id": [0],
  "magento_id": null
}
```

### 6.3 Respuesta del middleware (éxito)

```json
{
  "ok": true,
  "operation": "created",        // created | updated | deleted | noop
  "identifier": "envios-y-devoluciones",
  "magento_id": 123,
  "source": "odoo",
  "source_id": 42
}
```

- **Código HTTP:** `2xx` para éxito (Odoo acepta cualquier `2xx`).
- **Campo crítico:** Odoo **solo lee `magento_id`** de la respuesta (espera un integer) para persistirlo y marcar el registro como `synced`. El resto de campos (`ok`, `operation`, etc.) son informativos/recomendados pero Odoo no los exige.
- Si el `POST`/`PUT` no devuelve `magento_id`, Odoo igual marca `synced` pero no guardará el id, y el próximo guardado volverá a hacer `POST` (por eso conviene devolverlo siempre).

> **Nota sobre `GET`/`DELETE`:** en la Fase 1 Odoo **no** invoca estos métodos. Pueden implementarse pero no son necesarios para que el flujo funcione.

## 7. Mapeo de campos a la REST API de Magento

El middleware traduce el payload anterior a los repositorios CMS de Magento:

- **Pages:** `cmsPageRepositoryV1` → `POST/PUT /rest/V1/cmsPage`. Campos: `identifier`, `title`, `active`, `content`, `content_heading`, `page_layout`, `meta_title`, `meta_keywords`, `meta_description`, `sort_order`, `layout_update_xml`, `custom_theme`.
- **Blocks:** `cmsBlockRepositoryV1` → `POST/PUT /rest/V1/cmsBlock`. Campos: `identifier`, `title`, `active`, `content`.
- Para decidir create vs update, el middleware busca por `identifier` (search criteria de Magento) o usa su mapping local.

> El contenido HTML se envía tal cual. Las directivas/widgets de Magento (`{{...}}`) que se incluyan en `content` son responsabilidad del autor en Odoo.

## 8. Borrados y desactivación

- **Comportamiento actual de Odoo (Fase 1):** archivar un registro envía `active: false` por el flujo normal `POST`/`PUT`. **Odoo no llama a `DELETE`.**
- En consecuencia, FastAPI **no necesita** implementar borrado para la Fase 1; basta con respetar el flag `active` y mapearlo al campo `active` de Magento.

## 9. Errores e idempotencia

- **Idempotencia:** todas las escrituras son idempotentes por `identifier`. Reenviar el mismo payload no debe duplicar contenido (upsert).
- **Códigos de respuesta del middleware:**
  - `200/201` — aplicado en Magento.
  - `400` — payload inválido (faltan campos requeridos, `identifier` mal formado).
  - `401` — JWT ausente/expirado/ inválido → Odoo renueva token y reintenta.
  - `409` — conflicto de `identifier`.
  - `422` — Magento rechazó el contenido (devolver el mensaje de Magento en el body).
  - `502/503/504` — Magento no disponible → Odoo reintenta con backoff.
- **Cuerpo de error estándar** (este es el formato que Odoo sabe parsear):

```json
{ "ok": false, "error": { "code": "magento_error",
  "message": "...", "details": {} } }
```

  Odoo extrae el texto de error de `error.message`, con fallback a `message` en la raíz; si no encuentra ninguno, usa el cuerpo crudo. Devolver siempre un `error.message` legible para que aparezca claro en el campo "Último error" de Odoo.
- **Reintentos:** ante `401`, Odoo renueva el JWT y reintenta **una vez** automáticamente. Ante errores `5xx`/red, el registro queda en estado `error` y se reintenta manualmente con el botón "Sincronizar ahora" (el backoff automático queda como mejora futura).

## 10. Variables de entorno (por proyecto)

**Odoo** (`.env`):
```
MIDDLEWARE_BASE_URL=https://www.sebastianartaza.com/api/v1
MIDDLEWARE_CLIENT_ID=odoo-cms
MIDDLEWARE_CLIENT_SECRET=***
```

**FastAPI** (`.env`):
```
JWT_SECRET=***                       # o claves RS256
JWT_ISSUER=fastapi-middleware
JWT_TTL_SECONDS=3600
ODOO_CLIENTS={"odoo-cms": "<hash_secret>"}   # credenciales aceptadas
MAGENTO_BASE_URL=https://ituc.sebastianartaza.com
MAGENTO_ACCESS_TOKEN=***             # token admin / integración de Magento
```

**Magento:** crear una integración/usuario admin con permisos sobre *Content > CMS* y emitir el access token usado por FastAPI.

## 11. Estado y roadmap

- [x] **Odoo:** módulo `artaza_magento_connect` instalado — modelos CMS Page/Block, cliente JWT y push al guardar. **Hecho.**
- [ ] **FastAPI:** middleware (endpoints `/auth/token`, `/cms/pages`, `/cms/blocks` + integración con Magento). **Pendiente — este es el trabajo a hacer ahora.**
- [ ] **Magento:** crear la integración/token con permisos sobre *Content > CMS*.
- [ ] Soporte multi store-view (`store_id` por vista, traducciones).
- [ ] Catálogo: descripciones de productos y categorías.
- [ ] Webhooks Magento → Odoo (sincronización inversa), si se necesita.

## 12. Checklist de implementación para FastAPI

Lo que el middleware debe exponer para cerrar la Fase 1 (todo bajo el prefijo `…/api/v1`):

1. **`POST /auth/token`** — recibe `{grant_type, client_id, client_secret}`; valida contra `ODOO_CLIENTS`; devuelve `{access_token, token_type, expires_in}` con un JWT firmado (HS256/RS256, claims `sub`, `scope`, `exp`, `iat`). Ver §4.1.
2. **Dependencia de auth** — todas las rutas `/cms/*` exigen `Authorization: Bearer <JWT>`; si falta/expira/ inválido → `401` (Odoo renovará y reintentará una vez).
3. **`POST /cms/pages`** y **`PUT /cms/pages/{identifier}`** — upsert idempotente por `identifier` (ver tipos exactos en §5). Mapear a `cmsPageRepositoryV1` (§7).
4. **`POST /cms/blocks`** y **`PUT /cms/blocks/{identifier}`** — ídem contra `cmsBlockRepositoryV1`.
5. **Respuesta de éxito** — `2xx` con JSON que **incluya `magento_id` (integer)** (§6.3). El resto de campos son informativos.
6. **Errores** — usar el cuerpo `{ "ok": false, "error": { "code", "message", "details" } }` con un `error.message` legible (§9).
7. **Cliente Magento (§4.2)** — autenticarse con `MAGENTO_ACCESS_TOKEN` contra `MAGENTO_BASE_URL`; resolver create vs update buscando por `identifier` en Magento; devolver el `id` resultante como `magento_id`.
8. **No requerido en Fase 1:** `GET`/`DELETE` y backoff/cola (Odoo reintenta manualmente).

> Recordatorio de tipos que suelen romper el modelo Pydantic: `store_id` es **array de enteros**, `sort_order` es **string**, y `magento_id` puede ser **null**.

---
_Última actualización del contrato: 2026-06-24._
