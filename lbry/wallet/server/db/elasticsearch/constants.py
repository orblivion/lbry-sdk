INDEX_DEFAULT_SETTINGS = {
    "settings":
        {"analysis":
            {"analyzer": {
                "default": {"tokenizer": "whitespace", "filter": ["lowercase", "porter_stem"]}}},
            "index":
                {"refresh_interval": -1,
                 "number_of_shards": 1,
                 "number_of_replicas": 0,
                 "sort": {
                     "field": ["trending_score", "release_time"],
                     "order": ["desc", "desc"]
                 }}
        },
    "mappings": {
        "properties": {
            "claim_id": {
                "fields": {
                    "keyword": {
                        "ignore_above": 256,
                        "type": "keyword"
                    }
                },
                "type": "text",
                "index_prefixes": {
                    "min_chars": 1,
                    "max_chars": 10
                }
            },
            "sd_hash": {
                "fields": {
                    "keyword": {
                        "ignore_above": 96,
                        "type": "keyword"
                    }
                },
                "type": "text",
                "index_prefixes": {
                    "min_chars": 1,
                    "max_chars": 4
                }
            },
            "height": {"type": "integer"},
            "claim_type": {"type": "byte"},
            "censor_type": {"type": "byte"},
            "trending_score": {"type": "double"},
            "release_time": {"type": "long"}
        }
    }
}

FIELDS = {
    '_id',
    'claim_id', 'claim_type', 'claim_name', 'normalized_name',
    'tx_id', 'tx_nout', 'tx_position',
    'short_url', 'canonical_url',
    'is_controlling', 'last_take_over_height',
    'public_key_bytes', 'public_key_id', 'claims_in_channel',
    'channel_id', 'signature', 'signature_digest', 'is_signature_valid',
    'amount', 'effective_amount', 'support_amount',
    'fee_amount', 'fee_currency',
    'height', 'creation_height', 'activation_height', 'expiration_height',
    'stream_type', 'media_type', 'censor_type',
    'title', 'author', 'description',
    'timestamp', 'creation_timestamp',
    'duration', 'release_time',
    'tags', 'languages', 'has_source', 'reposted_claim_type',
    'reposted_claim_id', 'repost_count', 'sd_hash',
    'trending_score', 'tx_num'
}

TEXT_FIELDS = {'author', 'canonical_url', 'channel_id', 'description', 'claim_id', 'censoring_channel_id',
               'media_type', 'normalized_name', 'public_key_bytes', 'public_key_id', 'short_url', 'signature',
               'claim_name', 'signature_digest', 'title', 'tx_id', 'fee_currency', 'reposted_claim_id',
               'tags', 'sd_hash'}

RANGE_FIELDS = {
    'height', 'creation_height', 'activation_height', 'expiration_height',
    'timestamp', 'creation_timestamp', 'duration', 'release_time', 'fee_amount',
    'tx_position', 'repost_count', 'limit_claims_per_channel',
    'amount', 'effective_amount', 'support_amount',
    'trending_score', 'censor_type', 'tx_num'
}

ALL_FIELDS = RANGE_FIELDS | TEXT_FIELDS | FIELDS

REPLACEMENTS = {
    'claim_name': 'normalized_name',
    'name': 'normalized_name',
    'txid': 'tx_id',
    'nout': 'tx_nout',
    'trending_group': 'trending_score',
    'trending_mixed': 'trending_score',
    'trending_global': 'trending_score',
    'trending_local': 'trending_score',
    'reposted': 'repost_count',
    'stream_types': 'stream_type',
    'media_types': 'media_type',
    'valid_channel_signature': 'is_signature_valid'
}
