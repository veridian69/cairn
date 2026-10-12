package model

// AccountantRecord is the structured decision record attached to a proposal.
// It travels with the chat message but is rendered through accountant-specific
// views rather than as part of the conversational content.
type AccountantRecord struct {
	Proposer           string   `json:"proposer"`
	Idea               string   `json:"idea"`
	Assumptions        []string `json:"assumptions"`
	Evidence           string   `json:"evidence"`
	Probability        string   `json:"probability"`
	CapitalRequiredCHF *float64 `json:"capital_required_chf"`
	TimeRequired       string   `json:"time_required"`
	DownsideIfWrong    string   `json:"downside_if_wrong"`
	NextExperiment     string   `json:"next_experiment"`
	Dissent            string   `json:"dissent"`
}
