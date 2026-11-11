"""Build all recipe embeddings offline; run with the application server stopped."""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from app.config import Settings
from app.encoder import SentenceEncoder
from app.recipe_dense import build_recipe_dense


def main() -> None:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--index-dir', type=Path, default=settings.index_dir)
    parser.add_argument('--model-path', type=Path, default=settings.embedding_model_path)
    parser.add_argument('--device', default=settings.device)
    parser.add_argument('--batch-size', type=int, default=32)
    parser.add_argument('--chunk-size', type=int, default=256)
    parser.add_argument('--max-length', type=int, default=1024)
    parser.add_argument('--expected-count', type=int, default=544888)
    args = parser.parse_args()
    if args.batch_size < 1 or args.max_length < 1:
        parser.error('batch-size and max-length must be positive')
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    encoder = SentenceEncoder(args.model_path, args.device, args.max_length, args.batch_size)
    model = encoder.load()
    logging.info(json.dumps({'event': 'recipe_builder_start', 'device': str(model.device),
                             'batch_size': args.batch_size, 'max_length': args.max_length,
                             'chunk_size': args.chunk_size}))
    manifest = build_recipe_dense(args.index_dir, encoder, chunk_size=args.chunk_size,
                                  expected_count=args.expected_count)
    logging.info(json.dumps({'event': 'recipe_builder_complete', **manifest}))


if __name__ == '__main__':
    main()
