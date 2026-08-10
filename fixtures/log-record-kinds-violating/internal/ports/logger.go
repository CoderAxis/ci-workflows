package ports

// Logger violates rule 5: it re-declares the platform logger interface, and none of its methods
// takes a context.
//
// This is the shape the rule exists to reject, reproduced from the real thing. Twenty-two of these
// were in use across the platform, four inside platform-shared-go itself, each carrying a comment
// explaining that it was deliberately small so services could adapt their own logging to it. What
// they actually produced was records with no trace_id and no correlation_id - the schema is
// satisfied field for field and the line cannot be joined to the request that wrote it - plus five
// copies of the same LoggerAdapter in five repositories, bridging to an auditevent logger field
// that no code ever read.
//
// The fix is one line: `type Logger = logging.Logger`. An alias is not an interface declaration, so
// it does not match, which is deliberate.
type Logger interface {
	Debug(msg string, fields map[string]interface{})
	Info(msg string, fields map[string]interface{})
	Warn(msg string, fields map[string]interface{})
	Error(msg string, fields map[string]interface{})
}
