package main

import (
    "encoding/json"
	"strings"
	"testing"
)

func TestImportedMDLSupportsKeylessModelsParallelEdgesAndDiscriminators(t *testing.T) {
    ontology := demoOntology()
    ontology.Metrics = nil
    ontology.Objects[1].PrimaryKey = ""
    ontology.Relations = append(ontology.Relations, RelationClass{
        Name: "loan_other_branch", From: "loan", To: "branch", Cardinality: "N:1",
        Condition: "loan.branch_id = branch.id AND loan.status = 'jbo.acct.ACCT_LOAN'",
    })
    ontology.Metadata = map[string]any{"sourceSha256": "import-hash"}
    ontology.Relations[len(ontology.Relations)-1].Metadata = map[string]any{"evidenceStatus": "source_backed_not_production_validated"}
    if issues := validateOntology(ontology); len(issues) != 0 {
        t.Fatalf("keyless imported model should validate: %#v", issues)
    }
    artifact, issues := projectWren(ontology)
    if len(issues) != 0 { t.Fatalf("parallel relationships should project: %#v", issues) }
    navigations := map[string]bool{}
    for _, model := range artifact["models"].([]map[string]any) {
        if model["name"] == ontology.Objects[1].Name {
            if _, exists := model["primaryKey"]; exists { t.Fatal("must not fabricate a primary key") }
        }
        if model["name"] != "loan" { continue }
        for _, column := range model["columns"].([]map[string]any) {
            if relationship, ok := column["relationship"].(string); ok { navigations[relationship] = true }
        }
    }
    if !navigations["loan_belongs_to_branch"] || !navigations["loan_other_branch"] { t.Fatal("parallel edges were lost") }
    found := false
    for _, relationship := range artifact["relationships"].([]map[string]any) {
        if relationship["name"] == "loan_other_branch" {
            found = strings.Contains(relationship["condition"].(string), "'jbo.acct.ACCT_LOAN'")
        }
    }
    if !found { t.Fatal("SQL literal was rewritten as an identifier") }
    payload, _ := json.Marshal(ontology)
    var restored Ontology
    if err := json.Unmarshal(payload, &restored); err != nil { t.Fatal(err) }
    if restored.Metadata["sourceSha256"] != "import-hash" { t.Fatal("import provenance was dropped") }
}

func TestDemoOntologyValidatesAndProjectsEveryObject(t *testing.T) {
	ontology := demoOntology()
	if issues := validateOntology(ontology); len(issues) != 0 {
		t.Fatalf("demo ontology should be valid: %#v", issues)
	}
	artifact, projectionIssues := projectWren(ontology)
	if len(projectionIssues) != 0 {
		t.Fatalf("demo ontology should project: %#v", projectionIssues)
	}
	models := artifact["models"].([]map[string]any)
	if len(models) != len(ontology.Objects) {
		t.Fatalf("projection silently dropped objects: got %d want %d", len(models), len(ontology.Objects))
	}
	if artifact["catalog"] != "semora" || artifact["dataSource"] != "postgres" {
		t.Fatal("artifact must be a directly consumable Wren manifest")
	}
	cubes := artifact["cubes"].([]map[string]any)
	if len(cubes) == 0 || len(cubes[0]["measures"].([]map[string]any)) < len(ontology.Metrics) {
		t.Fatal("metrics must be projected to Wren cube measures")
	}
	for _, measure := range cubes[0]["measures"].([]map[string]any) {
		if strings.Contains(measure["expression"].(string), " WHERE ") {
			t.Fatal("Semora conditional aggregate syntax must be normalized to Wren SQL")
		}
	}
	metricNavigationFound := false
	conditionalHelperFound := false
	for _, model := range models {
		for _, column := range model["columns"].([]map[string]any) {
			if model["name"] == "branch" && column["name"] == "__calc_overdue_90_rate_numerator" && strings.Contains(column["expression"].(string), "loan.payment.__metric_overdue_90_rate_term_0") {
				metricNavigationFound = true
			}
			if model["name"] == "payment" && column["name"] == "__metric_overdue_90_rate_term_0" && strings.Contains(column["expression"].(string), "CASE WHEN days_overdue") {
				conditionalHelperFound = true
			}
		}
	}
	if !metricNavigationFound || !conditionalHelperFound {
		t.Fatal("multi-hop metrics must be projected as Wren calculated columns")
	}
}

func TestKnowledgeBaseBindingIsOptional(t *testing.T) {
    ontology := demoOntology()
    ontology.KnowledgeBaseIDs = nil
    if issues := validateOntology(ontology); len(issues) != 0 {
        t.Fatalf("knowledge must be optional: %#v", issues)
    }
}

func TestWrenProjectionIncludesHiddenRelationshipColumns(t *testing.T) {
	artifact, issues := projectWren(demoOntology())
	if len(issues) != 0 {
		t.Fatalf("projection should succeed: %#v", issues)
	}
	found := false
	navigationFound := false
	for _, model := range artifact["models"].([]map[string]any) {
		if model["name"] != "loan" {
			continue
		}
		for _, column := range model["columns"].([]map[string]any) {
			if column["name"] == "branch_id" {
				found = true
			}
			if column["name"] == "branch" && column["relationship"] == "loan_belongs_to_branch" {
				navigationFound = true
			}
		}
	}
	if !found {
		t.Fatal("relationship-only physical columns must not be silently dropped")
	}
	if !navigationFound {
		t.Fatal("relationships must be projected as navigable Wren columns")
	}
}

func TestWrenProjectionRejectsUnsafeTable(t *testing.T) {
	ontology := demoOntology()
	ontology.Objects[0].Table = "branches; DROP TABLE loans"
	_, issues := projectWren(ontology)
	if len(issues) == 0 {
		t.Fatal("unsafe Wren table mapping must block publication")
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
