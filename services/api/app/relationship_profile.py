"""Interpret sampled evidence without equating completeness with relationship existence."""
from .relationship_rules import MIN_REFERENCE_COVERAGE


def directions(profile):
    if profile.get('method') == 'bidirectional_random_membership_v4':
        return [profile.get('forward', {}), profile.get('reverse', {})]
    return [profile] if profile else []


def support(profile, cross_source=False):
    values = directions(profile)
    minimum = 200 if cross_source else 100
    sufficient = [p for p in values if p.get('source_nonnull_distinct_keys', 0) >= minimum
                  or p.get('source_exhausted') is True and p.get('source_nonnull_distinct_keys', 0) > 0]
    return {
        'sufficient': bool(sufficient),
        'high_coverage': any(p.get('directional_coverage', 0) >= MIN_REFERENCE_COVERAGE for p in sufficient),
        'positive_matches': any(p.get('matched_keys', 0) > 0 for p in values),
        'best_coverage': max((p.get('directional_coverage', 0) for p in values), default=0),
    }
