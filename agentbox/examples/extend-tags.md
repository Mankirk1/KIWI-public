Add tags to to-do items:
- An item can have zero or more tags (lowercase, 1-30 chars, letters/digits/hyphens).
- Create/update accept a `tags` list; responses include it.
- The list endpoint accepts `tag=<name>` (repeatable; items must have all given tags).
- `GET /tags` returns every tag in use with its item count, sorted by name.
Store tags in a separate SQLite table with a proper foreign key.
