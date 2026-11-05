package main

import "testing"

func sourceOntology() Ontology {
    return Ontology{Domain: "commerce", Name: "跨源", Version: "1", DataSourceID: "a",
        Objects: []ObjectClass{
            {Name: "client", Label: "客户", Table: "customers", DataSourceID: "a", PrimaryKey: "customer_id", Properties: []Property{{Name: "customer_id", Label: "编号", Column: "customer_id", Type: "string"}, {Name: "amount", Label: "金额", Column: "amount", Type: "decimal"}}},
            {Name: "balance", Label: "账户", Table: "balances", DataSourceID: "b", PrimaryKey: "customer_id", Properties: []Property{{Name: "customer_id", Label: "编号", Column: "customer_id", Type: "string"}, {Name: "amount", Label: "金额", Column: "amount", Type: "decimal"}}},
        }, Relations: []RelationClass{{Name: "client_balance", From: "client", To: "balance", Condition: "client.customer_id = balance.customer_id", Cardinality: "1:N"}},
    }
}

func TestCrossSourceProjectionKeepsLocalProjectsAndLookupEdge(t *testing.T) {
    ontology := sourceOntology()
    artifact, issues := projectWren(ontology)
    if len(issues) > 0 { t.Fatalf("unexpected issues: %#v", issues) }
    projects := artifact["sourceProjects"].(map[string]any)
    if len(projects) != 2 { t.Fatal("must publish two source projects") }
    for source, name := range map[string]string{"a": "client", "b": "balance"} {
        project := projects[source].(map[string]any)
        models := project["models"].([]map[string]any)
        if len(models) != 1 || models[0]["name"] != name { t.Fatal("project contains objects of another source") }
        if len(project["relationships"].([]map[string]any)) != 0 { t.Fatal("cross-source edge must not become a local JOIN") }
    }
    if len(artifact["crossSourceRelations"].([]RelationClass)) != 1 { t.Fatal("lookup relationship lost") }
}

func TestUnsupportedCrossSourceDefinitionsBlockPublication(t *testing.T) {
    for _, kind := range []string{"condition", "metric", "scope"} {
        ontology := sourceOntology()
        switch kind {
        case "condition": ontology.Relations[0].Condition += " AND client.amount > 0"
        case "metric": ontology.Metrics = []Metric{{Name: "bad", Label: "bad", Expression: "SUM(client.amount) + SUM(balance.amount)", Grain: "client"}}
        case "scope": ontology.SourceScopes = map[string]string{"b": "client.customer_id"}
        }
        _, issues := projectWren(ontology)
        if len(issues) == 0 { t.Fatalf("%s should block publication", kind) }
    }
}
