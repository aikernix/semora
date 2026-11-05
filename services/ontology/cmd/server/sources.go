package main

import (
    "fmt"
    "regexp"
    "strings"
)

func objectSource(object ObjectClass, ontology Ontology) string {
    if object.DataSourceID != "" { return object.DataSourceID }
    return ontology.DataSourceID
}

// A published multi-source artifact is a collection of immutable local MDLs.
// Cross-instance edges remain execution contracts, never a physical SQL JOIN.
func projectWren(ontology Ontology) (map[string]any, []ValidationIssue) {
    sources := map[string]bool{}
    objects := map[string]ObjectClass{}
    for _, object := range ontology.Objects {
        sources[objectSource(object, ontology)] = true
        objects[object.Name] = object
    }
    if len(sources) <= 1 { return projectWrenSingle(ontology) }
    _, issues := projectWrenSingle(ontology)
    for source, field := range ontology.SourceScopes {
        parts := strings.SplitN(field, ".", 2)
        valid := sources[source] && len(parts) == 2
        if valid {
            object, exists := objects[parts[0]]
            found := false
            for _, property := range object.Properties { if property.Name == parts[1] { found = true } }
            if object.PrimaryKey != "" && (parts[1] == "id" || parts[1] == object.PrimaryKey) { found = true }
            valid = exists && found && objectSource(object, ontology) == source
        }
        if !valid { issues = append(issues, ValidationIssue{"sourceScopes." + source, "机构范围字段必须属于该数据源已发布对象"}) }
    }
    edges := []RelationClass{}
    equality := regexp.MustCompile(`^\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*=\s*([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*$`)
    for index, relation := range ontology.Relations {
        if objectSource(objects[relation.From], ontology) == objectSource(objects[relation.To], ontology) { continue }
        match := equality.FindStringSubmatch(relation.Condition)
        valid := len(match) == 5
        if valid {
            valid = (match[1] == relation.From && match[3] == relation.To) || (match[1] == relation.To && match[3] == relation.From)
            for _, pair := range [][2]string{{match[1], match[2]}, {match[3], match[4]}} {
                found := false
                for _, property := range objects[pair[0]].Properties {
                    if pair[1] == property.Name || pair[1] == property.Column { found = true }
                }
                valid = valid && found
            }
        }
        if !valid { issues = append(issues, ValidationIssue{fmt.Sprintf("relations[%d]", index), "跨源关系必须使用两个对象已发布属性的单键等值条件"}) }
        edges = append(edges, relation)
    }
    for index, metric := range ontology.Metrics {
        source := objectSource(objects[metric.Grain], ontology)
        replaceSQLReferences(metric.Expression, func(reference string) string {
            parts := strings.SplitN(reference, ".", 2)
            if objectSource(objects[parts[0]], ontology) != source {
                issues = append(issues, ValidationIssue{fmt.Sprintf("metrics[%d]", index), "指标表达式不能跨数据源，请使用分步查询计划"})
            }
            return reference
        })
        for _, dimension := range metric.Dimensions {
            name := strings.SplitN(dimension, ".", 2)[0]
            if strings.Contains(dimension, ".") && objectSource(objects[name], ontology) != source {
                issues = append(issues, ValidationIssue{fmt.Sprintf("metrics[%d].dimensions", index), "指标维度不能跨数据源"})
            }
        }
    }
    projects := map[string]any{}
    for source := range sources {
        local := ontology
        local.DataSourceID = source
        local.Objects = []ObjectClass{}
        local.Relations = []RelationClass{}
        local.Metrics = []Metric{}
        local.DefaultFilters = []DefaultFilter{}
        for _, object := range ontology.Objects { if objectSource(object, ontology) == source { local.Objects = append(local.Objects, object) } }
        for _, relation := range ontology.Relations {
            if objectSource(objects[relation.From], ontology) == source && objectSource(objects[relation.To], ontology) == source { local.Relations = append(local.Relations, relation) }
        }
        for _, metric := range ontology.Metrics { if objectSource(objects[metric.Grain], ontology) == source { local.Metrics = append(local.Metrics, metric) } }
        for _, filter := range ontology.DefaultFilters {
            if objectSource(objects[strings.SplitN(filter.Field, ".", 2)[0]], ontology) == source { local.DefaultFilters = append(local.DefaultFilters, filter) }
        }
        local.ScopeField = ontology.SourceScopes[source]
        if local.ScopeField == "" && objectSource(objects[strings.SplitN(ontology.ScopeField, ".", 2)[0]], ontology) == source { local.ScopeField = ontology.ScopeField }
        if objectSource(objects[strings.SplitN(ontology.TimeField, ".", 2)[0]], ontology) != source { local.TimeField = "" }
        project, errors := projectWrenSingle(local)
        issues = append(issues, errors...)
        projects[source] = project
    }
    return map[string]any{"sourceProjects": projects, "crossSourceRelations": edges, "execution": "sequential_sources"}, issues
}
