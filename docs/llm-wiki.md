# LLM Wiki

`enmestador` alimenta un vault de LLM Wiki con material fuente limpio y
enriquecido.

## Rutas

Vault raiz:

```text
volumes/llm_wiki_seed
```

Entrada generada por `enmestador`:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks
```

Notas por fuente:

```text
volumes/llm_wiki_seed/Bookmarks/bookmarks/x
volumes/llm_wiki_seed/Bookmarks/bookmarks/linkedin
```

Wiki durable:

```text
volumes/llm_wiki_seed/wiki
```

## Contrato

`enmestador` no escribe directamente en `wiki/`. Escribe material fuente en
`Bookmarks/bookmarks/`.

La separacion es intencionada:

- `Bookmarks/bookmarks/` contiene una nota por bookmark.
- `raw/` conserva material crudo o importaciones no normalizadas.
- `wiki/` contiene conocimiento durable, curado por agentes o por revision
  humana.

## Flujo

1. `enmestador` trae material nuevo desde X/LinkedIn.
2. El LLM del pipeline resume y etiqueta cada pieza.
3. Obsidian o un agente lee `Bookmarks/bookmarks/**`.
4. Si una nota contiene una idea reutilizable, se promueve a `wiki/`:
   conceptos, entidades, claims, decisiones, mapas, preguntas, playbooks o
   sintesis.
5. El agente actualiza `wiki/index.md` y `wiki/log.md`.

En resumen: `Bookmarks/` es la bandeja de entrada enriquecida; `wiki/` es el
conocimiento destilado.

## Como Usarlo

Abre `volumes/llm_wiki_seed` como vault de Obsidian o sincronizalo con
Syncthing.

Revisa material nuevo en:

```text
Bookmarks/bookmarks/x
Bookmarks/bookmarks/linkedin
```

Cada nota incluye, cuando aplica:

- resumen;
- takeaway;
- tags;
- texto original;
- thread/articulo extraido;
- imagenes;
- links externos;
- tweets referenciados.

El frontmatter permite filtrar por:

- `source`;
- `url`;
- `tags`;
- `external_urls`;
- `referenced_tweet_urls`;
- `image_urls`.

Cada `.md` tiene un `.json` sidecar con el objeto completo para agentes que
necesiten datos estructurados.

## Flujo Para Agentes

```text
Read Bookmarks note
  -> identify reusable claim/concept/entity/question
  -> add/update wiki/* note
  -> link back to source URL or bookmark note
  -> update wiki/index.md if a new durable page exists
  -> append wiki/log.md with the maintenance action
```

Regla practica: una nota de `Bookmarks/` es evidencia o material de entrada, no
conocimiento final. El conocimiento final vive en `wiki/`.

## Dedupe

El pipeline deduplica notas del vault por URL canonica cuando `OUTPUT_DIR`
apunta a la carpeta de bookmarks del vault. Mantiene la nota mas rica y mueve
duplicados a:

```text
volumes/llm_wiki_seed/Bookmarks/dedupe_backup
```

El dedupe tiene en cuenta enriquecimiento, sidecar JSON, riqueza de contenido,
tamano Markdown y fecha de modificacion.
