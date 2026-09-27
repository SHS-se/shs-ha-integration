"""One durable record, parsed once and refreshed by its own saves."""


class DurableRecord:
    """Serve reads from the last saved record instead of re-reading its file.

    Reads share one parsed record, which callers must not change. A caller that
    changes the record loads a private copy and saves it back. Each save
    refreshes the shared record from the JSON it wrote, so readers see exactly
    what reloading the file would return, never a writer's unsaved changes.
    """

    def __init__(self, store, encode, decode):
        self._store = store
        self._encode = encode
        self._decode = decode
        self._record = None
        self._loaded = False

    async def async_read(self):
        """The shared record; treat it as read-only."""
        if not self._loaded:
            record = await self._store.async_load() or {}
            # A save that finished during this load already holds newer data.
            if not self._loaded:
                self._record, self._loaded = record, True
        return self._record

    async def async_load(self):
        """A private copy to change and pass to async_save."""
        return self._decode(self._encode(await self.async_read()))

    async def async_save(self, record):
        await self._store.async_save(record)
        try:
            self._record = self._decode(self._encode(record))
        except (TypeError, ValueError):
            # The store could not serialize it either; read what the file holds.
            self._record, self._loaded = None, False
            return
        self._loaded = True
