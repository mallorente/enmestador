# Pipeline Status — automations-somex

**Última ejecución confirmada:** 2026-05-14 22:45 UTC  
**Tests:** 225 ✅  
**Estado general:** Funcional con 4 bugs activos

---

## Qué funciona

| Componente | Estado |
|---|---|
| Scraper X (GraphQL interception) | ✅ Obtiene bookmarks |
| Scraper LinkedIn (API interception + DOM fallback) | ✅ Obtiene posts |
| LLM enrichment (kimi-k2.6 → fallbacks) | ✅ Funciona si hay contenido |
| Writer → Obsidian Markdown | ✅ |
| Notificaciones Telegram | ✅ Configurado |
| Docker / docker-compose | ✅ |
| Auth via cookies (x_cookies.txt + li_cookies.txt) | ✅ Cookies presentes |

---

## Bugs activos

### BUG-1 (CRÍTICO) — LinkedIn post_text = nombre del autor ✅ FIXED 2026-05-17
**Impacto:** 67/67 dead letters. El 100% de los posts de LinkedIn fallan en LLM enrichment.

El scraper captura posts de LinkedIn pero el campo `post_text` llega al LLM solo con el nombre del autor (ej. `"Atiq Rehman"`, `"Paweł Huryn"`). El LLM no tiene contenido real que resumir y agota todos los modelos del fallback chain.

- `dead_letter.jsonl`: 67 entradas, todas LinkedIn, todas `post_text <= 4 palabras`
- Causa probable: La estructura del response de la Voyager API de LinkedIn ha cambiado; `_extract_post_from_element` toma el nombre del autor de un campo `text` en lugar del cuerpo del post
- Cuando Playwright sí extrae el contenido completo (trafilatura fallback funciona en algunos casos), el LLM SÍ enriquece correctamente — ver `atiq-rehman.md` en `obsidian_output/`
- El problema es que el scraper no pasa ese contenido extraído como `post_text` al `Bookmark`; el `Bookmark.post_text` viene del API scraper, no del extractor posterior

**Fix necesario:** Revisar `_extract_post_from_element` en `scraper_linkedin.py` para que extraiga el texto del cuerpo del post correctamente del formato actual de la Voyager API.

---

### BUG-2 (MEDIO) — X bookmarks no se registran en `processed_urls.json` ✅ FIXED 2026-05-17
**Impacto:** Los bookmarks de X se reprocesarán en cada ejecución.

`volumes/state/processed_urls.json` contiene 73 URLs — todas de LinkedIn. Las 5 notas de X en `obsidian_output/` no tienen su URL registrada. Esto causa:
- Re-procesamiento de los mismos tweets en cada run
- Duplicados en los `.md` (collision append) — confirmado en `cline-sdk.md` que tiene el mismo post dos veces

Causa probable: La ejecución que procesó los bookmarks de X usó `STATE_DIR=./state` (directorio raíz, ahora huérfano), no `STATE_DIR=./volumes/state`.

**Fix necesario:** Verificar que el `.env` actual use `STATE_DIR=./volumes/state` consistentemente y migrar/limpiar `state/` del root.

---

### BUG-3 (MEDIO) — X.com t.co links extraen la página de error de X ✅ FIXED 2026-05-17
**Impacto:** Sección `## Article` en notas de X contiene ruido ("Something went wrong, but don't fret").

El filtro `_is_cookie_noise` existe y tiene el patrón correcto, pero la extracción vía Playwright de links t.co (que redirigen a X.com) igualmente deja pasar el error en algunos casos.

- Ver `introducing-the-cline-sdk-*.md` — `## Article: https://t.co/GbqWo4yiA6` contiene el texto de error
- El filtro en `_process_bookmark` (línea 262) sí usa `_is_cookie_noise`, pero puede que Playwright capture el contenido antes de que la página cargue el error

**Fix necesario:** Añadir una validación de longitud mínima de contenido extraído de links t.co además del cookie noise check.

---

### BUG-4 (BAJO) — `author_handle = "unknown"` en tweets de X ✅ FIXED 2026-05-17
**Impacto:** URLs tipo `x.com/unknown/status/...` en lugar de `x.com/@usuario/status/...`.

Ver `state/dead_letter.jsonl` (del root): tweets con URL `https://x.com/unknown/status/2039314742599151958`. El `_extract_author_handle` no encuentra `screen_name` en esos tweets y devuelve `"unknown"`.

**Fix necesario:** Mejorar rutas de extracción en `_extract_author_handle` o loggear el raw response para identificar la estructura real.

---

## Estado de directorios

```
volumes/
  state/
    processed_urls.json   → 73 URLs (solo linkedin)
    cursors.json          → cursor: null para ambas fuentes (bootstrap siempre)
    dead_letter.jsonl     → 67 entradas (todas LinkedIn, todas LLM failure)
  user_data/
    x_cookies.txt         ✅
    li_cookies.txt        ✅
  obsidian_output/        → 19 notas (14 LI + 5 X) — run reciente
  obsidian_output_2026-05-15/  → 50 notas (50 LI) — run anterior, backup manual

state/                    ← HUÉRFANO (STATE_DIR anterior)
  dead_letter.jsonl       → 5 errores de X (run de 2026-05-13, Pydantic validation)
```

**Limpieza pendiente:** El directorio `state/` del root puede borrarse una vez confirmado que es obsoleto.

---

## Próximos pasos sugeridos

1. **[BUG-1]** Debuggear la estructura real de la Voyager API de LinkedIn (usar `debug_li_requests.py`) y corregir `_extract_post_from_element`
2. **[BUG-2]** Comprobar que `STATE_DIR=./volumes/state` es consistente; migrar/eliminar `state/` root
3. **[BUG-3]** Añadir validación de contenido mínimo para external article extraction de X
4. **Primer commit** — el código está en buen estado para inicializar el historial git (excluir `.env` del commit)
5. **[OPCIONAL]** Implementar cursor funcional para LinkedIn (actualmente no hay paginación guardada)
