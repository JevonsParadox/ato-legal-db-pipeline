-- ============================================================================
-- Indexes — built separately so the schema file stays readable
-- ============================================================================

-- Documents
create index idx_documents_doc_type        on documents (doc_type);
create index idx_documents_status          on documents (status);
create index idx_documents_year            on documents (year);
create index idx_documents_short_code      on documents using gin (short_code gin_trgm_ops);
create index idx_documents_date_issued     on documents (date_issued desc);
create index idx_documents_date_modified   on documents (date_last_modified desc);
create index idx_documents_fts             on documents using gin (fts);
-- pgvector — ivfflat is good for our scale (~50–100k docs). Tune lists as the
-- corpus grows; rule of thumb is sqrt(rowcount).
create index idx_documents_embedding       on documents using ivfflat (embedding vector_cosine_ops) with (lists = 200);

-- Paragraphs (semantic search target)
create index idx_paragraphs_document_id    on document_paragraphs (document_id);
create index idx_paragraphs_fts            on document_paragraphs using gin (fts);
create index idx_paragraphs_embedding      on document_paragraphs using ivfflat (embedding vector_cosine_ops) with (lists = 1000);

-- Versions
create index idx_versions_document_id      on document_versions (document_id, version_number desc);

-- Relationships (citation graph)
create index idx_relationships_source      on document_relationships (source_document_id, relationship_type);
create index idx_relationships_target      on document_relationships (target_document_id, relationship_type);
create index idx_relationships_target_raw  on document_relationships (target_docid_raw);

-- Compendium submissions (lookup by parent ruling or by the EC doc itself)
create index idx_compendium_parent         on compendium_submissions (parent_document_id);
create index idx_compendium_doc            on compendium_submissions (compendium_document_id, ordinal);

-- Legislation graph
create index idx_legref_document           on legislation_references (document_id);
create index idx_legref_act                on legislation_references (act_id);
create index idx_legref_section            on legislation_references (section_id);
create index idx_legref_lookup             on legislation_references (act_short_name, section_raw);

-- Case-law graph
create index idx_caseref_document          on case_references (document_id);
create index idx_caseref_citation          on case_references using gin (citation gin_trgm_ops);
create index idx_caseref_case_name         on case_references using gin (case_name gin_trgm_ops);

-- Topics
create index idx_topics_path               on topics using gin (path);
create index idx_doctopic_topic            on document_topics (topic_id);

-- Change events
create index idx_change_events_detected    on change_events (detected_at desc);
create index idx_change_events_document    on change_events (document_id, detected_at desc);

-- Fetch queue
create index idx_fetch_queue_status        on fetch_queue (status, priority, scheduled_for);
