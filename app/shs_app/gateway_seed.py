"""Seed an inert HA gateway from a verified dormant import, under the source lease."""
from contextlib import closing
import json

from shs_core.command_journal import CommandJournal, WriterLease, entry_paths
from shs_core.gateway_journal import GatewayJournal
from .migration_import import regular_path, verify_import
from .migration_export import existing_file, file_digest, command_facts


def seed_gateway(imported, ha_storage, *, require_core_stopped):
    require_core_stopped()
    imported, ha_storage = regular_path(imported), regular_path(ha_storage)
    proof_path = existing_file(imported/'import.json')
    proof_bytes = proof_path.read_bytes()
    proof = json.loads(proof_bytes)
    entry = proof['entry_id']
    source_path, lock_path = entry_paths(ha_storage, entry)
    # Core retains this source lease for its lifetime, even after unload/failure.
    with WriterLease(lock_path):
        require_core_stopped()
        verify_import(imported, proof)
        authority = CommandJournal(existing_file(source_path)).state()
        if (authority['owner'] != 'fenced' or authority['migration_id'] != proof['migration_id']
                or authority['release'] != proof['source_release'] or authority['clean_stop'] != 1):
            raise ValueError('Live source does not match the sealed import')
        provenance = imported/'gateway_seed'
        command_path, _ = entry_paths(provenance, entry)
        if file_digest(source_path) != file_digest(command_path):
            # A SQLite backup can have different bytes. Compare canonical logical evidence.
            if command_facts(source_path) != command_facts(command_path):
                raise ValueError('Live sealed command evidence differs from the imported source')
        for prefix in ('controller', 'battery_writer'):
            name = f'shs_energy.{prefix}.{entry}'
            if file_digest(existing_file(ha_storage/name)) != file_digest(existing_file(provenance/name)):
                raise ValueError('Live ownership evidence differs from the imported source')
        ownership = json.loads((provenance/f'shs_energy.controller.{entry}').read_bytes())['data']
        writer = json.loads((provenance/f'shs_energy.battery_writer.{entry}').read_bytes())['data']
        if proof_path.read_bytes() != proof_bytes:
            raise ValueError('Import proof changed during gateway seed verification')
        identity = {key: proof[key] for key in ('entry_id', 'migration_id', 'export_sha256', 'pair')}
        destination = ha_storage/f'shs_energy.gateway.{entry}.sqlite'
        require_core_stopped()
        with closing(CommandJournal(command_path).connect(readonly=True)) as source:
            GatewayJournal.seed(destination, identity, ownership, writer, source)
        return destination
