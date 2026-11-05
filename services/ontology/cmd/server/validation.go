package main

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

const wrenArtifactVersion = "wren-core-mdl-0.8.2+semora.8"

var (
	wrenIdentifierPattern = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]*$`)
	wrenReferencePattern  = regexp.MustCompile(`\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b`)
	wrenConditionalSum    = regexp.MustCompile(`(?i)SUM\(([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\s+WHERE\s+(.+?)\)`)
	wrenSumDenominator    = regexp.MustCompile(`(?i)/\s*(SUM\([^)]+\))`)
	wrenAggregateRatio    = regexp.MustCompile(`(?i)^\s*(SUM\(.*?\))\s*/\s*(SUM\(.*\))\s*$`)
)

func validateOntology(ontology Ontology) []ValidationIssue {
	issues := []ValidationIssue{}
	if strings.TrimSpace(ontology.Domain) == "" {
		issues = append(issues, ValidationIssue{"domain", "业务域不能为空"})
	}
	if strings.TrimSpace(ontology.Name) == "" {
		issues = append(issues, ValidationIssue{"name", "本体名称不能为空"})
	}
	if strings.TrimSpace(ontology.Version) == "" {
		issues = append(issues, ValidationIssue{"version", "版本不能为空"})
	}
	if strings.TrimSpace(ontology.DataSourceID) == "" {
		issues = append(issues, ValidationIssue{"dataSourceId", "必须绑定已登记的数据源"})
	}
    if len(ontology.KnowledgeBaseIDs) > 0 {
		for index, value := range ontology.KnowledgeBaseIDs {
			if strings.TrimSpace(value) == "" {
				issues = append(issues, ValidationIssue{fmt.Sprintf("knowledgeBaseIds[%d]", index), "知识库 ID 不能为空"})
			}
		}
	}
	if len(ontology.Objects) == 0 {
		issues = append(issues, ValidationIssue{"objects", "至少需要一个对象类"})
	}
	objects := map[string]ObjectClass{}
	for index, object := range ontology.Objects {
		path := fmt.Sprintf("objects[%d]", index)
        if object.Name == "" || object.Label == "" || object.Table == "" {
            issues = append(issues, ValidationIssue{path, "对象名、中文标签和物理表映射必须完整"})
		}
		if _, exists := objects[object.Name]; exists {
			issues = append(issues, ValidationIssue{path + ".name", "对象名重复"})
		}
		objects[object.Name] = object
		properties := map[string]bool{}
		for propertyIndex, property := range object.Properties {
			propertyPath := fmt.Sprintf("%s.properties[%d]", path, propertyIndex)
			if property.Name == "" || property.Label == "" || property.Column == "" || property.Type == "" {
				issues = append(issues, ValidationIssue{propertyPath, "属性名、中文标签、列映射和类型必须完整"})
			}
			if properties[property.Name] {
				issues = append(issues, ValidationIssue{propertyPath + ".name", "属性名重复"})
			}
			properties[property.Name] = true
		}
	}
	relations := map[string]bool{}
	validCardinality := map[string]bool{"1:1": true, "1:N": true, "N:1": true, "N:N": true}
	for index, relation := range ontology.Relations {
		path := fmt.Sprintf("relations[%d]", index)
		if relation.Name == "" || relation.Condition == "" {
			issues = append(issues, ValidationIssue{path, "关系名和连接条件必须完整"})
		}
		if relations[relation.Name] {
			issues = append(issues, ValidationIssue{path + ".name", "关系名重复"})
		}
		relations[relation.Name] = true
		if _, ok := objects[relation.From]; !ok {
			issues = append(issues, ValidationIssue{path + ".from", "起点对象不存在"})
		}
		if _, ok := objects[relation.To]; !ok {
			issues = append(issues, ValidationIssue{path + ".to", "终点对象不存在"})
		}
		if !validCardinality[relation.Cardinality] {
			issues = append(issues, ValidationIssue{path + ".cardinality", "基数必须是 1:1、1:N、N:1 或 N:N"})
		}
	}
	metrics := map[string]bool{}
	for index, metric := range ontology.Metrics {
		path := fmt.Sprintf("metrics[%d]", index)
		if metric.Name == "" || metric.Label == "" || metric.Expression == "" || metric.Grain == "" {
			issues = append(issues, ValidationIssue{path, "指标名、中文标签、表达式和粒度必须完整"})
		}
		if metrics[metric.Name] {
			issues = append(issues, ValidationIssue{path + ".name", "指标名重复"})
		}
		metrics[metric.Name] = true
		if _, ok := objects[metric.Grain]; !ok {
			issues = append(issues, ValidationIssue{path + ".grain", "指标粒度对象不存在"})
		}
	}
	validateFieldReference := func(path, reference string, allowEmpty bool) {
		if reference == "" && allowEmpty {
			return
		}
		parts := strings.Split(reference, ".")
		if len(parts) != 2 {
			issues = append(issues, ValidationIssue{path, "字段必须使用 object.property 格式"})
			return
		}
		object, ok := objects[parts[0]]
		if !ok {
			issues = append(issues, ValidationIssue{path, "字段所属对象不存在"})
			return
		}
        if parts[1] == "id" && object.PrimaryKey != "" {
			return
		}
		for _, property := range object.Properties {
			if property.Name == parts[1] {
				return
			}
		}
		issues = append(issues, ValidationIssue{path, "字段属性不存在"})
	}
	validateFieldReference("scopeField", ontology.ScopeField, true)
	validateFieldReference("timeField", ontology.TimeField, true)
	validOperators := map[string]bool{"eq": true, "ne": true, "gt": true, "gte": true, "lt": true, "lte": true, "in": true, "between": true}
	for index, filter := range ontology.DefaultFilters {
		path := fmt.Sprintf("defaultFilters[%d]", index)
		parts := strings.Split(filter.Field, ".")
		if len(parts) != 2 {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤字段必须使用 object.property 格式"})
			continue
		}
		object, ok := objects[parts[0]]
		if !ok {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤对象不存在"})
			continue
		}
		propertyFound := false
		for _, property := range object.Properties {
			if property.Name == parts[1] {
				propertyFound = true
				break
			}
		}
		if !propertyFound {
			issues = append(issues, ValidationIssue{path + ".field", "默认过滤属性不存在"})
		}
		if !validOperators[filter.Operator] {
			issues = append(issues, ValidationIssue{path + ".operator", "默认过滤运算符不受支持"})
		}
	}
	return issues
}

func canonicalOntology(input Ontology) Ontology {
	result := input
	result.Objects = append([]ObjectClass(nil), input.Objects...)
	result.Relations = append([]RelationClass(nil), input.Relations...)
	result.Metrics = append([]Metric(nil), input.Metrics...)
	sort.Slice(result.Objects, func(i, j int) bool { return result.Objects[i].Name < result.Objects[j].Name })
	for index := range result.Objects {
		result.Objects[index].Properties = append([]Property(nil), result.Objects[index].Properties...)
		sort.Slice(result.Objects[index].Properties, func(i, j int) bool {
			return result.Objects[index].Properties[i].Name < result.Objects[index].Properties[j].Name
		})
	}
	sort.Slice(result.Relations, func(i, j int) bool { return result.Relations[i].Name < result.Relations[j].Name })
	sort.Slice(result.Metrics, func(i, j int) bool { return result.Metrics[i].Name < result.Metrics[j].Name })
	return result
}

func wrenType(value string) string {
	switch strings.ToLower(value) {
	case "string", "text":
		return "varchar"
	case "number", "float":
		return "double"
	case "datetime":
		return "timestamp"
	case "bool":
		return "boolean"
	case "int":
		return "integer"
	default:
		return strings.ToLower(value)
	}
}

// Only rewrite identifiers outside SQL string literals. Type-discriminator
// values such as 'jbo.acct.ACCT_LOAN' must survive projection unchanged.
func replaceSQLReferences(value string, replace func(string) string) string {
    pattern := regexp.MustCompile(`'(?:''|\\.|[^'])*'|\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\b`)
    return pattern.ReplaceAllStringFunc(value, func(token string) string {
        if strings.HasPrefix(token, "'") { return token }
        return replace(token)
    })
}

func wrenTableReference(value string) (map[string]any, bool) {
	parts := strings.Split(value, ".")
	if len(parts) == 1 && wrenIdentifierPattern.MatchString(parts[0]) {
		return map[string]any{"schema": "public", "table": parts[0]}, true
	}
	if len(parts) == 2 && wrenIdentifierPattern.MatchString(parts[0]) && wrenIdentifierPattern.MatchString(parts[1]) {
		return map[string]any{"schema": parts[0], "table": parts[1]}, true
	}
	return nil, false
}

func projectWrenSingle(ontology Ontology) (map[string]any, []ValidationIssue) {
	issues := []ValidationIssue{}
	objects := map[string]ObjectClass{}
	propertyColumns := map[string]map[string]Property{}
	columnTypes := map[string]map[string]string{}
	relationshipColumns := map[string]map[string]map[string]any{}
	for index, object := range ontology.Objects {
		objects[object.Name] = object
		propertyColumns[object.Name] = map[string]Property{}
		columnTypes[object.Name] = map[string]string{}
		relationshipColumns[object.Name] = map[string]map[string]any{}
		if !wrenIdentifierPattern.MatchString(object.Name) {
			issues = append(issues, ValidationIssue{fmt.Sprintf("objects[%d].name", index), "对象名不是 Wren 安全标识符"})
		}
        if object.PrimaryKey != "" && !wrenIdentifierPattern.MatchString(object.PrimaryKey) {
			issues = append(issues, ValidationIssue{fmt.Sprintf("objects[%d].primaryKey", index), "主键不是 Wren 安全标识符"})
        } else if object.PrimaryKey != "" {
			columnTypes[object.Name][object.PrimaryKey] = "integer"
		}
		if _, ok := wrenTableReference(object.Table); !ok {
			issues = append(issues, ValidationIssue{fmt.Sprintf("objects[%d].table", index), "物理表只能使用 table 或 schema.table 安全标识符"})
		}
		for propertyIndex, property := range object.Properties {
			if !wrenIdentifierPattern.MatchString(property.Name) || !wrenIdentifierPattern.MatchString(property.Column) {
				issues = append(issues, ValidationIssue{fmt.Sprintf("objects[%d].properties[%d]", index, propertyIndex), "属性名和物理列必须是 Wren 安全标识符"})
				continue
			}
			propertyColumns[object.Name][property.Name] = property
			columnTypes[object.Name][property.Column] = wrenType(property.Type)
		}
	}

	physicalReference := func(objectName, fieldName string, allowHidden bool) (string, string, bool) {
		object, exists := objects[objectName]
		if !exists {
			return "", "", false
		}
        if object.PrimaryKey != "" && (fieldName == "id" || fieldName == object.PrimaryKey) {
			return object.PrimaryKey, columnTypes[objectName][object.PrimaryKey], true
		}
		if property, exists := propertyColumns[objectName][fieldName]; exists {
			return property.Column, wrenType(property.Type), true
		}
		if !allowHidden || !wrenIdentifierPattern.MatchString(fieldName) {
			return "", "", false
		}
		return fieldName, "integer", true
	}

	relations := make([]map[string]any, 0, len(ontology.Relations))
	cardinality := map[string]string{"1:1": "ONE_TO_ONE", "1:N": "ONE_TO_MANY", "N:1": "MANY_TO_ONE", "N:N": "MANY_TO_MANY"}
	reverseCardinality := map[string]string{"1:1": "ONE_TO_ONE", "1:N": "MANY_TO_ONE", "N:1": "ONE_TO_MANY", "N:N": "MANY_TO_MANY"}
	for index, relation := range ontology.Relations {
		conditionValid := true
        condition := replaceSQLReferences(relation.Condition, func(reference string) string {
			parts := strings.SplitN(reference, ".", 2)
			column, dataType, ok := physicalReference(parts[0], parts[1], true)
			if !ok {
				conditionValid = false
				return reference
			}
			columnTypes[parts[0]][column] = dataType
			return parts[0] + "." + column
		})
		if !wrenIdentifierPattern.MatchString(relation.Name) || !conditionValid {
			issues = append(issues, ValidationIssue{fmt.Sprintf("relations[%d]", index), "关系名或连接条件无法完整投影到 Wren"})
		}
		relations = append(relations, map[string]any{
			"name": relation.Name, "models": []string{relation.From, relation.To},
			"joinType": cardinality[relation.Cardinality], "condition": condition,
		})
		reverseName := relation.Name + "__reverse"
		relations = append(relations, map[string]any{
			"name": reverseName, "models": []string{relation.To, relation.From},
			"joinType": reverseCardinality[relation.Cardinality], "condition": condition,
		})
		for _, navigation := range []struct {
			source       string
			target       string
			relationship string
		}{{relation.From, relation.To, relation.Name}, {relation.To, relation.From, reverseName}} {
			source, target := navigation.source, navigation.target
			if _, exists := objects[source]; !exists {
				continue
			}
			if _, exists := propertyColumns[source][target]; exists {
				issues = append(issues, ValidationIssue{fmt.Sprintf("relations[%d]", index), "关系导航名与对象属性重名，无法投影到 Wren"})
				continue
			}
            navigationName := target
            if existing, exists := relationshipColumns[source][target]; exists && existing["relationship"] != navigation.relationship {
                navigationName = target + "__" + navigation.relationship
            }
            relationshipColumns[source][navigationName] = map[string]any{
                "name": navigationName, "type": target, "relationship": navigation.relationship,
			}
		}
	}

	cubesByGrain := map[string][]Metric{}
	cubeMeasures := map[string][]map[string]any{}
	calculatedColumns := map[string][]map[string]any{}
	calculatedColumnNames := map[string]map[string]bool{}
	for objectName := range objects {
		calculatedColumnNames[objectName] = map[string]bool{}
	}
	for metricIndex, metric := range ontology.Metrics {
		cubesByGrain[metric.Grain] = append(cubesByGrain[metric.Grain], metric)
		expressionValid := wrenIdentifierPattern.MatchString(metric.Name)
		conditionalIndex := 0
		expression := wrenConditionalSum.ReplaceAllStringFunc(metric.Expression, func(conditional string) string {
			matches := wrenConditionalSum.FindStringSubmatch(conditional)
			if len(matches) != 3 {
				expressionValid = false
				return conditional
			}
			valueParts := strings.SplitN(matches[1], ".", 2)
			objectName := valueParts[0]
			helperName := fmt.Sprintf("__metric_%s_term_%d", metric.Name, conditionalIndex)
			conditionalIndex++
			helperExpression := "CASE WHEN " + matches[2] + " THEN " + matches[1] + " ELSE 0 END"
			helperExpression = wrenReferencePattern.ReplaceAllStringFunc(helperExpression, func(reference string) string {
				parts := strings.SplitN(reference, ".", 2)
				if parts[0] != objectName {
					expressionValid = false
					return reference
				}
				column, dataType, ok := physicalReference(parts[0], parts[1], false)
				if !ok {
					expressionValid = false
					return reference
				}
				columnTypes[parts[0]][column] = dataType
				return column
			})
			if _, exists := calculatedColumnNames[objectName]; !exists {
				expressionValid = false
				return conditional
			}
			calculatedColumnNames[objectName][helperName] = true
			calculatedColumns[objectName] = append(calculatedColumns[objectName], map[string]any{
				"name": helperName, "type": "double", "isCalculated": true, "expression": helperExpression,
			})
			return "SUM(" + objectName + "." + helperName + ")"
		})
		expression = wrenReferencePattern.ReplaceAllStringFunc(expression, func(reference string) string {
			parts := strings.SplitN(reference, ".", 2)
			column := parts[1]
			if !calculatedColumnNames[parts[0]][parts[1]] {
				physicalColumn, dataType, ok := physicalReference(parts[0], parts[1], false)
				if !ok {
					expressionValid = false
					return reference
				}
				column = physicalColumn
				columnTypes[parts[0]][column] = dataType
			}
			if parts[0] == metric.Grain {
				return column
			}
			path, _, found := findRelationshipPath(ontology, metric.Grain, parts[0])
			if !found || len(path) < 2 {
				expressionValid = false
				return reference
			}
			return strings.Join(path[1:], ".") + "." + column
		})
		if !expressionValid {
			issues = append(issues, ValidationIssue{fmt.Sprintf("metrics[%d].expression", metricIndex), "指标名或表达式无法完整投影到 Wren"})
		}
		properties := map[string]any{"label": metric.Label, "format": metric.Format, "aliases": metric.Aliases}
		if ratio := wrenAggregateRatio.FindStringSubmatch(expression); len(ratio) == 3 {
			numeratorColumn := "__calc_" + metric.Name + "_numerator"
			denominatorColumn := "__calc_" + metric.Name + "_denominator"
			numeratorMeasure := "__measure_" + metric.Name + "_numerator"
			denominatorMeasure := "__measure_" + metric.Name + "_denominator"
			for _, value := range []struct {
				name       string
				expression string
			}{{numeratorColumn, ratio[1]}, {denominatorColumn, ratio[2]}} {
				calculatedColumnNames[metric.Grain][value.name] = true
				calculatedColumns[metric.Grain] = append(calculatedColumns[metric.Grain], map[string]any{
					"name": value.name, "type": "double", "isCalculated": true, "expression": value.expression,
				})
			}
			cubeMeasures[metric.Grain] = append(cubeMeasures[metric.Grain],
				map[string]any{"name": numeratorMeasure, "expression": "SUM(" + numeratorColumn + ")", "type": "double"},
				map[string]any{"name": denominatorMeasure, "expression": "SUM(" + denominatorColumn + ")", "type": "double"},
			)
			derived := numeratorMeasure + " / NULLIF(" + denominatorMeasure + ", 0)"
			if metric.Format == "percentage" {
				derived = "100.0 * (" + derived + ")"
			}
			cubeMeasures[metric.Grain] = append(cubeMeasures[metric.Grain], map[string]any{
				"name": metric.Name, "expression": derived, "type": "double", "properties": properties,
			})
		} else {
			expression = wrenSumDenominator.ReplaceAllString(expression, "/ NULLIF($1, 0)")
			if metric.Format == "percentage" {
				expression = "100.0 * (" + expression + ")"
			}
			columnName := "__metric_" + metric.Name
			calculatedColumnNames[metric.Grain][columnName] = true
			calculatedColumns[metric.Grain] = append(calculatedColumns[metric.Grain], map[string]any{
				"name": columnName, "type": "double", "isCalculated": true, "expression": expression,
			})
			cubeMeasures[metric.Grain] = append(cubeMeasures[metric.Grain], map[string]any{
				"name": metric.Name, "expression": "SUM(" + columnName + ")", "type": "double", "properties": properties,
			})
		}
	}

	models := make([]map[string]any, 0, len(ontology.Objects))
	for _, object := range ontology.Objects {
		names := make([]string, 0, len(columnTypes[object.Name]))
		for name := range columnTypes[object.Name] {
			names = append(names, name)
		}
		sort.Strings(names)
		columns := make([]map[string]any, 0, len(names))
		for _, name := range names {
			column := map[string]any{"name": name, "type": columnTypes[object.Name][name]}
			if name == object.PrimaryKey {
				column["notNull"] = true
			}
			columns = append(columns, column)
		}
		navigationNames := make([]string, 0, len(relationshipColumns[object.Name]))
		for name := range relationshipColumns[object.Name] {
			navigationNames = append(navigationNames, name)
		}
		sort.Strings(navigationNames)
		for _, name := range navigationNames {
			columns = append(columns, relationshipColumns[object.Name][name])
		}
		calculated := append([]map[string]any(nil), calculatedColumns[object.Name]...)
		sort.Slice(calculated, func(i, j int) bool { return calculated[i]["name"].(string) < calculated[j]["name"].(string) })
		for _, column := range calculated {
			columns = append(columns, column)
		}
		tableReference, _ := wrenTableReference(object.Table)
        model := map[string]any{
			"name": object.Name, "tableReference": tableReference,
            "columns": columns,
        }
        if object.PrimaryKey != "" { model["primaryKey"] = object.PrimaryKey }
        models = append(models, model)
	}

	grains := make([]string, 0, len(cubesByGrain))
	for grain := range cubesByGrain {
		grains = append(grains, grain)
	}
	sort.Strings(grains)
	cubes := make([]map[string]any, 0, len(grains))
	for _, grain := range grains {
		measures := cubeMeasures[grain]
		dimensions := []map[string]any{}
		for _, property := range objects[grain].Properties {
			if property.Sensitive {
				continue
			}
			dimensions = append(dimensions, map[string]any{
				"name": property.Name, "expression": property.Column, "type": wrenType(property.Type),
				"properties": map[string]any{"label": property.Label, "aliases": property.Aliases},
			})
		}
		cube := map[string]any{"name": grain + "_metrics", "baseObject": grain, "measures": measures}
		if len(dimensions) > 0 {
			cube["dimensions"] = dimensions
		}
		cubes = append(cubes, cube)
	}
	return map[string]any{
		"catalog": "semora", "schema": "public", "dataSource": "postgres",
		"models": models, "relationships": relations, "cubes": cubes,
	}, issues
}

func findRelationshipPath(ontology Ontology, from, to string) ([]string, []RelationClass, bool) {
	type step struct {
		object    string
		relations []RelationClass
		objects   []string
	}
	queue := []step{{from, nil, []string{from}}}
	visited := map[string]bool{from: true}
	for len(queue) > 0 {
		current := queue[0]
		queue = queue[1:]
		if current.object == to {
			return current.objects, current.relations, true
		}
		for _, relation := range ontology.Relations {
			next := ""
			if relation.From == current.object {
				next = relation.To
			} else if relation.To == current.object {
				next = relation.From
			}
			if next == "" || visited[next] {
				continue
			}
			visited[next] = true
			queue = append(queue, step{
				next,
				append(append([]RelationClass{}, current.relations...), relation),
				append(append([]string{}, current.objects...), next),
			})
		}
	}
	return nil, nil, false
}
