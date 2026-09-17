package memory

import "sort"

const rrfConstant = 60

type RecallRequest struct {
	Query       string
	QueryVector []float32
	Model       string
	Limit       int
	QueryBytes  int
	Exclude     map[string]bool
}

func (s *Store) Recall(request RecallRequest) ([]Item, error) {
	if request.Limit <= 0 {
		return nil, nil
	}
	fetch := 3 * request.Limit
	if fetch < 10 {
		fetch = 10
	}
	prefix := QueryPrefix(request.Query, request.QueryBytes)
	var textResults []Item
	if query := BuildFTSQuery(prefix); query != "" {
		var err error
		textResults, err = s.searchBM25(query, fetch)
		if err != nil {
			return nil, err
		}
	}
	var vectorResults []Item
	if len(request.QueryVector) > 0 {
		var err error
		vectorResults, _, err = s.searchVector(request.QueryVector, request.Model, fetch)
		if err != nil {
			return nil, err
		}
	}
	fused := rrfMerge(textResults, vectorResults)
	results := make([]Item, 0, request.Limit)
	for _, item := range fused {
		if request.Exclude[item.SourceMessageID] {
			continue
		}
		results = append(results, item)
		if len(results) == request.Limit {
			break
		}
	}
	return results, nil
}

func rrfMerge(lists ...[]Item) []Item {
	scores := make(map[string]float64)
	items := make(map[string]Item)
	for _, list := range lists {
		for index, item := range list {
			scores[item.ID] += 1 / float64(rrfConstant+index+1)
			items[item.ID] = item
		}
	}
	merged := make([]Item, 0, len(items))
	for _, item := range items {
		merged = append(merged, item)
	}
	sort.Slice(merged, func(i, j int) bool {
		left, right := scores[merged[i].ID], scores[merged[j].ID]
		if left != right {
			return left > right
		}
		if !merged[i].SourceCreatedAt.Equal(merged[j].SourceCreatedAt) {
			return merged[i].SourceCreatedAt.After(merged[j].SourceCreatedAt)
		}
		return merged[i].ID < merged[j].ID
	})
	return merged
}

func (s *Store) Pinned(maximum int, exclude map[string]bool) ([]Item, error) {
	if maximum <= 0 {
		return nil, nil
	}
	items, err := s.queryItems(`SELECT ` + itemColumns + `
		FROM memories WHERE pinned = 1 ORDER BY created_at ASC, id ASC`)
	if err != nil {
		return nil, err
	}
	results := make([]Item, 0, maximum)
	for _, item := range items {
		if exclude[item.SourceMessageID] {
			continue
		}
		results = append(results, item)
		if len(results) == maximum {
			break
		}
	}
	return results, nil
}
