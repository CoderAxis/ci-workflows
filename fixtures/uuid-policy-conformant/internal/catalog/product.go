// Package catalog is the precision pin for the accessor inference, and the most
// important file in this fixture. Domain entities carry an `ID`, mint it with a
// v4, and no document states a version for it. Treating the field NAME `ID` as an
// event id sink was measured at 613 findings across the fleet — Product, Plan,
// Subscription, User — none of them a violation of anything. The inference must
// therefore key on the accessor: nothing here declares an EventID() method, so
// nothing here is the policy's business.
package catalog

import "github.com/google/uuid"

// Product is an aggregate, not an event. Its ID is entity identity.
type Product struct {
	ID    uuid.UUID
	OrgID uuid.UUID
	Name  string
}

// Slug is a getter that returns a field of its own type, which is the shape the
// inference reads — but it is not named after any sink, so it establishes
// nothing. The inference is keyed on the sink set, not on "any accessor".
func (p Product) Slug() string { return p.Name }

// New mints a v4 entity id, which is legitimate and undocumented.
func New(orgID uuid.UUID, name string) Product {
	return Product{ID: uuid.New(), OrgID: orgID, Name: name}
}

// Plan is a second entity, because the fleet-wide noise this pins was mostly the
// same shape repeated across domain packages.
type Plan struct {
	ID   uuid.UUID
	Code string
}

func NewPlan(code string) Plan {
	id := uuid.New()
	return Plan{ID: id, Code: code}
}
