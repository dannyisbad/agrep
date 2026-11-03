#!/usr/bin/env python3
"""Export the scale fixture's stored geometry, then publish it with 0.4.9 APIs.

This is synthetic retrieval setup, not 200k model inferences. All inputs and
outputs must be supplied explicitly and remain in task scratch directories.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

import numpy as np

ap=argparse.ArgumentParser()
ap.add_argument("phase",choices=["export","publish"])
ap.add_argument("--data",type=Path)
ap.add_argument("--matrix",type=Path,required=True)
ap.add_argument("--reference",type=Path)
ap.add_argument("--lane",choices=["cpu","metal"],default="metal")
a=ap.parse_args()
assert os.environ.get("AGREP_HOME") and os.environ.get("AGREP_DATA_DIR")
start=time.perf_counter()
if a.phase=="export":
    connection=sqlite3.connect(f"file:{a.data.resolve()}/index.db?mode=ro",uri=True)
    rows=connection.execute("SELECT p.session,p.turn,v.block,v.slot FROM semantic_refs r JOIN prose p ON p.id=r.row_id JOIN semantic_chunks v ON v.content_key=r.content_key").fetchall()
    assert len(rows)==200000
    output=np.memmap(a.matrix,mode="w+",dtype="<f2",shape=(200000,384))
    maps={}
    for session,turn,block,slot in rows:
        if block not in maps:
            maps[block]=np.memmap(a.data/'vectors'/block,mode='r',dtype='u1').reshape(-1,1156)
        i=int(session.split('-')[1])*50+turn
        output[i]=maps[block][slot,388:].view('<f2')
    output.flush()
    print(json.dumps(dict(rows=len(rows),seconds=time.perf_counter()-start,geometry="F16 vectors exported from the 0.5 scale fixture; same vectors for both versions")))
else:
    sys.path.insert(0,str(a.reference.resolve()/'py'))
    import common,embedder,embedding_segments,semantic,semantic_segment_build
    rows=list(common.iter_messages())
    assert len(rows)==200000
    matrix=np.memmap(a.matrix,mode='r',dtype='<f2',shape=(200000,384))
    indices=[int(row.text.rsplit('Experiment ',1)[1].rstrip('.')) for row in rows]
    values=matrix[indices].astype('<f4')
    values/=np.linalg.norm(values,axis=1,keepdims=True)
    ids=[row.id for row in rows]
    hashes=[common.semantic_text_hash(row.text) for row in rows]
    model=embedder.profile_string(embedder.LANE_METAL if a.lane=='metal' else embedder.LANE_CPU)
    with semantic_segment_build.prepare([values],ids,hashes,rows,dim=384,model_id=model) as prepared:
        # Use the reference's file-backed adoption API to avoid keeping a second
        # full vector bundle on this disk during publication.
        id_file=prepared.root/'rows.ids'
        hash_file=prepared.root/'rows.hashes'
        ref_file=prepared.root/'rows.refs.sqlite'
        embedding_segments._line_file(id_file,ids)
        embedding_segments._line_file(hash_file,hashes)
        embedding_segments._refs_db(ref_file,prepared.refs,ids,hashes,0)
        result=embedding_segments.publish_base(common.DATA_DIR/'embeddings.meta',source=semantic.source_generation(),model_id=model,dim=384,artifacts=prepared.artifacts,ids=id_file,hashes=hash_file,refs=ref_file,coverage={'total':len(rows),'order':'newest-first'},_adopt_inputs=True)
    print(json.dumps(dict(rows=len(rows),seconds=time.perf_counter()-start,coverage=result['coverage'],lane=a.lane,model=model)))
