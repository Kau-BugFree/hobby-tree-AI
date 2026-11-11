import json
from pathlib import Path
import pytest
from app.knowledge import load_knowledge, KnowledgeRecord
from app.retrieval import Retrieval
from test_retrieval import corpus, FakeEncoder, FakeReranker


def write_knowledge(settings, **changes):
    row = dict(knowledge_id='dressing:steps', activity_ids=['dressing'], title='간장 드레싱 단계',
               kind='steps', content='간장 드레싱 만들기 준비\n간장 드레싱 만들기 완료',
               source_refs=['dressing/steps'], review_status='source_only')
    row.update(changes)
    (settings.data_dir/'coaching_knowledge.jsonl').write_text(json.dumps(row,ensure_ascii=False),encoding='utf-8')
    return row


@pytest.mark.parametrize('changes', [dict(activity_ids=['unknown']), dict(related_knowledge_ids=['missing']),
    dict(content='invented instruction'), dict(source_refs=['../../secret']), dict(knowledge_id='dressing'),
    dict(review_status='reviewed'), dict(kind='unknown')])
def test_bad_references_and_fabricated_provenance_rejected(corpus, changes):
    settings, _ = corpus; write_knowledge(settings, **changes)
    with pytest.raises(ValueError):
        load_knowledge(settings.data_dir)


def test_knowledge_search_scoped_and_not_selectable(corpus):
    settings, _ = corpus; write_knowledge(settings)
    service = Retrieval(settings)
    assert service.search('간장 드레싱', scope='knowledge', constraints={'activity_id':'dressing'})['items']
    assert not service.search('간장 드레싱', scope='knowledge', constraints={'activity_id':'bread'})['items']
    assert service.get('dressing:steps') is None
    assert service.get('dressing')


def test_draft_excluded_and_removed_file_invalidates_index(corpus):
    settings, _ = corpus; write_knowledge(settings, review_status='draft')
    service = Retrieval(settings)
    assert not service.search('드레싱', scope='knowledge')['items']
    write_knowledge(settings)
    assert Retrieval(settings).search('드레싱', scope='knowledge')['items']
    (settings.data_dir/'coaching_knowledge.jsonl').unlink()
    assert not Retrieval(settings).search('드레싱', scope='knowledge')['items']


def test_duplicate_knowledge_rejected(corpus):
    settings, _ = corpus; row = write_knowledge(settings)
    with (settings.data_dir/'coaching_knowledge.jsonl').open('a', encoding='utf-8') as f:
        f.write('\n'+json.dumps(row))
    with pytest.raises(ValueError):
        load_knowledge(settings.data_dir)


def test_knowledge_hybrid_path_uses_existing_models(corpus):
    settings, _ = corpus; write_knowledge(settings)
    settings.embedding_enabled = settings.reranker_enabled = True
    encoder = FakeEncoder(); reranker = FakeReranker('드레싱')
    result = Retrieval(settings, encoder=encoder, reranker=reranker).search('드레싱',scope='knowledge')
    assert result['diagnostics']['search_mode'] == 'hybrid_bm25_dense_rrf'
    assert result['diagnostics']['reranker'] == 'cross_encoder'
    restarted = Retrieval(settings, encoder=encoder, reranker=reranker).search('드레싱', scope='knowledge')
    assert restarted['diagnostics']['dense_candidates'] > 0
