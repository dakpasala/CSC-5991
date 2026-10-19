// Mirrors window_features.py's _distribution(): min/max/mean/median/p25/p75
// over a slice of float64 values. Percentile/median use the same linear
// interpolation numpy's np.percentile/np.median use by default, so results
// match the Python pipeline's bit-for-bit (within floating point tolerance),
// not just approximately.
package core

import (
	"math"
	"sort"
)

type Distribution struct {
	Min    float64
	Max    float64
	Mean   float64
	Median float64
	P25    float64
	P75    float64
}

// NaNDistribution mirrors _distribution([]) -- every stat is NaN when
// there are zero values to summarize; a count of zero is a real answer,
// but there's no "median of nothing" to report honestly.
func NaNDistribution() Distribution {
	return Distribution{Min: math.NaN(), Max: math.NaN(), Mean: math.NaN(), Median: math.NaN(), P25: math.NaN(), P75: math.NaN()}
}

// ComputeDistribution mirrors _distribution(values).
func ComputeDistribution(values []float64) Distribution {
	if len(values) == 0 {
		return NaNDistribution()
	}

	sorted := make([]float64, len(values))
	copy(sorted, values)
	sort.Float64s(sorted)

	sum := 0.0
	for _, v := range sorted {
		sum += v
	}

	return Distribution{
		Min:    sorted[0],
		Max:    sorted[len(sorted)-1],
		Mean:   sum / float64(len(sorted)),
		Median: numpyPercentile(sorted, 50),
		P25:    numpyPercentile(sorted, 25),
		P75:    numpyPercentile(sorted, 75),
	}
}

// numpyPercentile replicates numpy's default ("linear") percentile
// interpolation on an ALREADY-SORTED ascending slice.
func numpyPercentile(sorted []float64, p float64) float64 {
	n := len(sorted)
	if n == 1 {
		return sorted[0]
	}
	h := (p / 100.0) * float64(n-1)
	lo := int(math.Floor(h))
	hi := int(math.Ceil(h))
	if lo == hi {
		return sorted[lo]
	}
	frac := h - float64(lo)
	return sorted[lo] + frac*(sorted[hi]-sorted[lo])
}
