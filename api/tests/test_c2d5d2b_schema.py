"""0043 upgrade preserves historic ledger; runtime does not acquire new privileges."""
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import upgrade


def test_0043_upgrade_preserves_legacy_ledger(empty_db_url):
    upgrade(empty_db_url,'0042')
    engine=create_engine(empty_db_url,hide_parameters=True)
    with engine.begin() as con:  # labelled migration setup, never worker credentials
        item=con.scalar(text("INSERT INTO library_items(visibility,publication_state,title) VALUES('common','listed','synthetic') RETURNING id"))
        version=con.scalar(text("INSERT INTO paper_versions(item_id,version_label) VALUES(:i,'v1') RETURNING id"),{'i':item})
        file=con.scalar(text("INSERT INTO source_files(item_id,version_id,verified_mime,storage_key,byte_size,checksum_sha256) VALUES(:i,:v,'text/markdown','synthetic',1,repeat('a',64)) RETURNING id"),{'i':item,'v':version})
        call=con.scalar(text("INSERT INTO embedding_calls(source_file_id,model_id,provider,pipeline_version,chunk_hash,status,reserved_usd,cost_status,reserved_day,lease_until) VALUES(:f,'openai/text-embedding-3-small','openrouter','synthetic',repeat('b',64),'uncertain',0.01,'unknown_reserved_ceiling',CURRENT_DATE,NULL) RETURNING id"),{'f':file})
        before=con.scalar(text('SELECT row_to_json(c)::text FROM embedding_calls c WHERE id=:i'),{'i':call})
    upgrade(empty_db_url,'0043')  # this revision's own contract; later heads are tested separately
    with engine.connect() as con:
        row=con.execute(text('SELECT reservation_token,reservation_job_id,reservation_job_token,reserved_usd,status FROM embedding_calls WHERE id=:i'),{'i':call}).one()
        assert row[:3]==(None,None,None) and str(row.reserved_usd)=='0.01000000' and row.status=='uncertain'
        assert con.scalar(text("SELECT version_num FROM alembic_version"))=='0043'
    with engine.begin() as con, pytest.raises(IntegrityError):
        con.execute(text('UPDATE embedding_calls SET reservation_token=:n WHERE id=:i'),{'n':uuid.uuid4(),'i':call})
    engine.dispose()
