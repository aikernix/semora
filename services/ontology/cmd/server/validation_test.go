package main

import "testing"

func TestOfflineOnlyDomainPublishesAndUnbindsWithoutExecutionConnection(t *testing.T) {
	id := "3884f2863dd14b52a05ad2215208618b"
	value := Ontology{Domain: "offline", Name: "离线", Version: "1.0.0", OfflineSourceIDs: []string{id}}
	snapshot, err := preparePublication(value)
	if err != nil || snapshot.Ontology.DataSourceID != "" || len(snapshot.Ontology.OfflineSourceIDs) != 1 {
		t.Fatalf("offline-only configuration should publish as a reference: %#v %v", snapshot, err)
	}
	if stripped := withoutSource(value, id); len(stripped.OfflineSourceIDs) != 0 || len(value.OfflineSourceIDs) != 1 {
		t.Fatal("unbind must only remove the reference from the new configuration")
	}
	value.DataSourceID = id
	if _, err = preparePublication(value); err == nil {
		t.Fatal("offline reference must not be used as an execution connection")
	}
}

func TestNewBindingsCannotMixOnlineAndOfflineOrAddSecondOffline(t *testing.T) {
	first, second := "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
	offline := Ontology{OfflineSourceIDs: []string{first}}
	online := Ontology{SourcePackageRefs: []SourcePackageRef{{DataSourceID: "online"}}}
	mixed := Ontology{OfflineSourceIDs: offline.OfflineSourceIDs, SourcePackageRefs: online.SourcePackageRefs}
	for _, pair := range [][2]Ontology{{offline, mixed}, {online, mixed}, {offline, {OfflineSourceIDs: []string{first, second}}}} {
		if err := checkSourceBindingChange(pair[0], pair[1]); err == nil {
			t.Fatalf("incompatible binding change accepted: %#v", pair)
		}
	}
	if err := checkSourceBindingChange(mixed, offline); err != nil {
		t.Fatalf("legacy conflict must allow unbinding: %v", err)
	}
	if err := checkSourceBindingChange(mixed, mixed); err != nil {
		t.Fatalf("legacy conflict must not block unchanged bindings: %v", err)
	}
}

func TestKnowledgeBaseBindingIsOptional(t *testing.T) {
	ontology := demoOntology()
	ontology.KnowledgeBaseIDs = nil
	if issues := validateOntology(ontology); len(issues) != 0 {
		t.Fatalf("knowledge must be optional: %#v", issues)
	}
}

func TestValidationRejectsUnsafeTable(t *testing.T) {
	ontology := demoOntology()
	ontology.Objects[0].Table = "objects; DROP TABLE records"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unsafe mapping accepted")
	}
}

func TestValidationRejectsUnknownRelationshipEndpoint(t *testing.T) {
	ontology := demoOntology()
	ontology.Relations[0].To = "missing_object"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unknown relationship endpoint must block publication")
	}
}

func TestRelationshipPathFindsCustomerToBranch(t *testing.T) {
	objects, relations, found := findRelationshipPath(demoOntology(), "customer", "branch")
	if !found || len(relations) != 2 {
		t.Fatalf("expected two-hop path, found=%v relations=%d", found, len(relations))
	}
	if objects[0] != "customer" || objects[len(objects)-1] != "branch" {
		t.Fatalf("unexpected path: %#v", objects)
	}
}

func TestMetricCandidateUpdatesDraftAndBumpsPatchVersion(t *testing.T) {
	ontology := demoOntology()
	originalCount := len(ontology.Metrics)
	err := applyCandidate(&ontology, "metric", map[string]any{
		"name": "customer_count", "label": "贷款客户数", "expression": "COUNT(customer.id)",
		"grain": "branch", "dimensions": []any{"branch"}, "format": "integer",
	})
	if err != nil {
		t.Fatalf("candidate should apply: %v", err)
	}
	if len(ontology.Metrics) != originalCount+1 {
		t.Fatalf("candidate should add one metric")
	}
	if issues := validateOntology(ontology); len(issues) != 0 {
		t.Fatalf("candidate result should remain valid: %#v", issues)
	}
	if got := bumpPatchVersion("1.2.9"); got != "1.2.10" {
		t.Fatalf("unexpected bumped version: %s", got)
	}
}

func TestInvalidPropertyCandidateDoesNotApply(t *testing.T) {
	ontology := demoOntology()
	err := applyCandidate(&ontology, "property", map[string]any{
		"object": "missing", "property": map[string]any{
			"name": "x", "column": "x", "type": "string",
		},
	})
	if err == nil {
		t.Fatal("missing target object should be rejected")
	}
}

func TestValidationRejectsUnknownScopeField(t *testing.T) {
	ontology := demoOntology()
	ontology.ScopeField = "missing.id"
	if issues := validateOntology(ontology); len(issues) == 0 {
		t.Fatal("unknown scope field must block publication")
	}
}
