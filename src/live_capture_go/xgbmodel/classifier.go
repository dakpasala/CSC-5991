package xgbmodel

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Classifier is a Model plus the files train_xgboost_ablation.py --final
// saves next to it: classes.json (class index -> label) and
// <name>.features.json (input column order, cross-checked against the
// feature names embedded in the model itself).
type Classifier struct {
	*Model
	Classes []string
}

func LoadClassifier(modelPath string) (*Classifier, error) {
	m, err := Load(modelPath)
	if err != nil {
		return nil, err
	}
	var classes []string
	if err := readJSON(filepath.Join(filepath.Dir(modelPath), "classes.json"), &classes); err != nil {
		return nil, err
	}
	if len(classes) != m.NumClass {
		return nil, fmt.Errorf("classes.json lists %d classes, model has %d", len(classes), m.NumClass)
	}
	featuresPath := strings.TrimSuffix(modelPath, filepath.Ext(modelPath)) + ".features.json"
	if _, err := os.Stat(featuresPath); err == nil {
		var features []string
		if err := readJSON(featuresPath, &features); err != nil {
			return nil, err
		}
		if strings.Join(features, ",") != strings.Join(m.Features, ",") {
			return nil, fmt.Errorf("%s does not match the feature names inside the model", featuresPath)
		}
	}
	return &Classifier{Model: m, Classes: classes}, nil
}

func readJSON(path string, v any) error {
	b, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	if err := json.Unmarshal(b, v); err != nil {
		return fmt.Errorf("parsing %s: %w", path, err)
	}
	return nil
}

// ColumnIndex maps each model feature to its position in a row's header,
// so full stats rows (all ~130 columns, any order) can be scored directly.
func (c *Classifier) ColumnIndex(header []string) ([]int, error) {
	pos := make(map[string]int, len(header))
	for i, h := range header {
		pos[h] = i
	}
	idx := make([]int, len(c.Features))
	var missing []string
	for i, f := range c.Features {
		p, ok := pos[f]
		if !ok {
			missing = append(missing, f)
		}
		idx[i] = p
	}
	if len(missing) > 0 {
		return nil, fmt.Errorf("rows are missing features the model needs: %v", missing)
	}
	return idx, nil
}

// Prediction is one scored row.
type Prediction struct {
	Label         string
	Confidence    float64
	Probabilities []float64 // aligned with Classes
}

// ScoreRow scores a row of string values (an empty cell is missing, the
// same convention as the CSVs and core.WindowRow.Values()), using a column
// index from ColumnIndex.
func (c *Classifier) ScoreRow(values []string, idx []int) Prediction {
	x := make([]float64, len(idx))
	for i, p := range idx {
		x[i] = parseCell(values[p])
	}
	probs := c.PredictProba(x)
	best := 0
	for k := range probs {
		if probs[k] > probs[best] {
			best = k
		}
	}
	return Prediction{Label: c.Classes[best], Confidence: probs[best], Probabilities: probs}
}

func parseCell(s string) float64 {
	if s == "" {
		return math.NaN()
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return math.NaN() // same as pandas' to_numeric(errors="coerce")
	}
	return v
}
