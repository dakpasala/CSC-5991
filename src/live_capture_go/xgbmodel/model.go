// Package xgbmodel evaluates an XGBoost multiclass model saved in XGBoost's
// native JSON format (train_xgboost_ablation.py --final) directly in Go, so
// the router build classifies windows with no Python, no xgboost library,
// and no cgo.
//
// Supported exactly as the models in this project use it: gbtree booster,
// objective multi:softprob, numerical splits only (categorical splits are
// rejected at load time rather than silently mis-evaluated). Scoring follows
// XGBoost's own CPU predictor so outputs match its predict_proba:
//   - inputs are cast to float32, like XGBoost's DMatrix does;
//   - a split sends x left iff x < threshold; a missing value (NaN) follows
//     the node's default_left;
//   - each class's margin starts at its base_score and accumulates its trees'
//     leaf values in float32, in tree order;
//   - probabilities are a float32 softmax over the class margins.
//
// If the model was saved with an early-stopping best_iteration attribute,
// only trees up to that iteration are used, matching the sklearn wrapper's
// predict. Final models are refit without early stopping and use every tree.
//
// Memory: all trees are flattened into one []node slice (16 bytes per node).
// The final option-C model (3,625 trees, 818,839 nodes) is ~13MB in RAM,
// against 50.7MB for its JSON file.
package xgbmodel

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strconv"
	"strings"
)

type node struct {
	value       float32 // split threshold for an inner node, leaf value for a leaf
	left, right int32   // absolute indices into Model.nodes; left == -1 marks a leaf
	feature     int32
	defaultLeft bool
}

type Model struct {
	Features  []string // input order the model expects
	NumClass  int
	baseScore []float32 // one per class
	nodes     []node
	roots     []int32 // root node index of each tree
	treeClass []int32 // class each tree adds to
}

// Load reads a model JSON file.
func Load(path string) (*Model, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	var raw struct {
		Learner struct {
			Attributes   map[string]string `json:"attributes"`
			FeatureNames []string          `json:"feature_names"`
			Objective    struct {
				Name string `json:"name"`
			} `json:"objective"`
			LearnerModelParam struct {
				BaseScore string `json:"base_score"`
				NumClass  string `json:"num_class"`
				NumFeat   string `json:"num_feature"`
			} `json:"learner_model_param"`
			GradientBooster struct {
				Name  string `json:"name"`
				Model struct {
					GbtreeModelParam struct {
						NumParallelTree string `json:"num_parallel_tree"`
					} `json:"gbtree_model_param"`
					TreeInfo        []int32 `json:"tree_info"`
					IterationIndptr []int   `json:"iteration_indptr"`
					Trees           []struct {
						LeftChildren    []int32   `json:"left_children"`
						RightChildren   []int32   `json:"right_children"`
						SplitIndices    []int32   `json:"split_indices"`
						SplitConditions []float32 `json:"split_conditions"`
						DefaultLeft     []uint8   `json:"default_left"`
						SplitType       []uint8   `json:"split_type"`
					} `json:"trees"`
				} `json:"model"`
			} `json:"gradient_booster"`
		} `json:"learner"`
	}
	if err := json.NewDecoder(f).Decode(&raw); err != nil {
		return nil, fmt.Errorf("parsing %s: %w", path, err)
	}
	l := raw.Learner
	if l.GradientBooster.Name != "gbtree" {
		return nil, fmt.Errorf("unsupported booster %q (only gbtree)", l.GradientBooster.Name)
	}
	if l.Objective.Name != "multi:softprob" {
		return nil, fmt.Errorf("unsupported objective %q (only multi:softprob)", l.Objective.Name)
	}
	numClass, err := strconv.Atoi(l.LearnerModelParam.NumClass)
	if err != nil || numClass < 2 {
		return nil, fmt.Errorf("bad num_class %q", l.LearnerModelParam.NumClass)
	}
	numFeat, err := strconv.Atoi(l.LearnerModelParam.NumFeat)
	if err != nil || len(l.FeatureNames) != numFeat {
		return nil, fmt.Errorf("model has %d feature names but num_feature %q", len(l.FeatureNames), l.LearnerModelParam.NumFeat)
	}
	baseScore, err := parseBaseScore(l.LearnerModelParam.BaseScore, numClass)
	if err != nil {
		return nil, err
	}

	gb := l.GradientBooster.Model
	trees := gb.Trees
	if len(gb.TreeInfo) != len(trees) {
		return nil, fmt.Errorf("tree_info has %d entries for %d trees", len(gb.TreeInfo), len(trees))
	}
	if best, ok := l.Attributes["best_iteration"]; ok {
		it, err := strconv.Atoi(best)
		if err != nil || it+1 >= len(gb.IterationIndptr) {
			return nil, fmt.Errorf("bad best_iteration %q", best)
		}
		trees = trees[:gb.IterationIndptr[it+1]]
	}

	m := &Model{Features: l.FeatureNames, NumClass: numClass, baseScore: baseScore}
	for t, tr := range trees {
		n := len(tr.LeftChildren)
		if len(tr.RightChildren) != n || len(tr.SplitIndices) != n || len(tr.SplitConditions) != n || len(tr.DefaultLeft) != n {
			return nil, fmt.Errorf("tree %d: inconsistent array lengths", t)
		}
		offset := int32(len(m.nodes))
		m.roots = append(m.roots, offset)
		m.treeClass = append(m.treeClass, gb.TreeInfo[t])
		for i := 0; i < n; i++ {
			if len(tr.SplitType) == n && tr.SplitType[i] != 0 {
				return nil, fmt.Errorf("tree %d node %d: categorical splits are not supported", t, i)
			}
			nd := node{value: tr.SplitConditions[i], left: -1, right: -1, feature: tr.SplitIndices[i], defaultLeft: tr.DefaultLeft[i] != 0}
			if tr.LeftChildren[i] != -1 {
				if nd.feature < 0 || int(nd.feature) >= numFeat {
					return nil, fmt.Errorf("tree %d node %d: feature index %d out of range", t, i, nd.feature)
				}
				nd.left = offset + tr.LeftChildren[i]
				nd.right = offset + tr.RightChildren[i]
			}
			m.nodes = append(m.nodes, nd)
		}
	}
	return m, nil
}

// parseBaseScore handles both the per-class vector form XGBoost 3.x writes
// ("[-7.39E-1,4.31E-1,...]") and the older single-scalar form.
func parseBaseScore(s string, numClass int) ([]float32, error) {
	s = strings.TrimSpace(s)
	parts := []string{s}
	if strings.HasPrefix(s, "[") {
		parts = strings.Split(strings.Trim(s, "[]"), ",")
	}
	out := make([]float32, numClass)
	for k := range out {
		p := parts[0]
		if len(parts) == numClass {
			p = parts[k]
		} else if len(parts) != 1 {
			return nil, fmt.Errorf("base_score %q has %d values for %d classes", s, len(parts), numClass)
		}
		v, err := strconv.ParseFloat(strings.TrimSpace(p), 32)
		if err != nil {
			return nil, fmt.Errorf("bad base_score %q: %w", s, err)
		}
		out[k] = float32(v)
	}
	return out, nil
}

// NumNodes is the total node count across all trees used for prediction.
func (m *Model) NumNodes() int { return len(m.nodes) }

// NumTrees is the number of trees used for prediction.
func (m *Model) NumTrees() int { return len(m.roots) }

// PredictProba returns one probability per class for one row. x must hold
// one value per m.Features entry, in that order; NaN means missing.
func (m *Model) PredictProba(x []float64) []float64 {
	xf := make([]float32, len(x))
	for i, v := range x {
		xf[i] = float32(v) // NaN stays NaN
	}
	margins := make([]float32, m.NumClass)
	copy(margins, m.baseScore)
	for t, root := range m.roots {
		i := root
		for {
			nd := &m.nodes[i]
			if nd.left == -1 {
				margins[m.treeClass[t]] += nd.value
				break
			}
			v := xf[nd.feature]
			switch {
			case v != v: // NaN: missing
				if nd.defaultLeft {
					i = nd.left
				} else {
					i = nd.right
				}
			case v < nd.value:
				i = nd.left
			default:
				i = nd.right
			}
		}
	}
	return softmax(margins)
}

func softmax(margins []float32) []float64 {
	maxM := margins[0]
	for _, v := range margins[1:] {
		if v > maxM {
			maxM = v
		}
	}
	exps := make([]float32, len(margins))
	var sum float32
	for k, v := range margins {
		exps[k] = float32(math.Exp(float64(v - maxM)))
		sum += exps[k]
	}
	out := make([]float64, len(margins))
	for k := range exps {
		out[k] = float64(exps[k] / sum)
	}
	return out
}
