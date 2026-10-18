"""Maps each financial concept we need to the XBRL tags companies use for it.

Why a priority list instead of one tag per concept?
Companies don't all use the same tag for the same number, and a single
company can switch tags over time. The big example is revenue: when the
ASC 606 accounting standard took effect (2018), most companies moved from
"SalesRevenueNet" / "Revenues" to "RevenueFromContractWithCustomer...".
For every fiscal year we take the FIRST tag in the list that has a value,
record which tag was used, and flag it when the tag changes between years.

missing_severity says what a gap means:
  error   -> the model can't work without it
  warning -> usable, but a human should check (e.g. no debt tag: is debt
             really zero, or tagged under a name we don't know?)
  info    -> optional detail, often legitimately absent
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Concept:
    name: str
    tags: tuple[str, ...]
    period: str            # "duration" (flow over the year) or "instant" (balance at year-end)
    unit: str              # "USD" or "shares"
    missing_severity: str  # "error" | "warning" | "info"
    description: str


CONCEPTS: tuple[Concept, ...] = (
    # ---------------- Income statement (duration) ----------------
    # "Revenues" comes first because it's the reported top line. The ASC 606
    # tag covers only customer-contract revenue, so for payroll companies it
    # leaves out interest earned on client funds (Paylocity: ~$120M/yr).
    Concept("revenue",
            ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
             "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
             "SalesRevenueServicesNet"),
            "duration", "USD", "error",
            "Top line. Drives EV/Revenue, growth and the Rule of 40."),
    Concept("cost_of_revenue",
            ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfServices"),
            "duration", "USD", "warning",
            "Hosting, support, etc. Needed to derive gross margin if gross profit isn't tagged."),
    Concept("gross_profit", ("GrossProfit",), "duration", "USD", "info",
            "Many SaaS filers don't tag it; Stage 3 derives it as revenue - cost of revenue."),
    Concept("operating_income", ("OperatingIncomeLoss",), "duration", "USD", "error",
            "EBIT. Starting point for EBITDA (EBIT + D&A)."),
    Concept("net_income", ("NetIncomeLoss", "ProfitLoss"), "duration", "USD", "error",
            "Bottom line."),
    Concept("income_tax", ("IncomeTaxExpenseBenefit",), "duration", "USD", "info",
            "Used to sanity-check the tax rate assumption in the DCF."),
    Concept("interest_expense",
            ("InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt"),
            "duration", "USD", "info",
            "Helps estimate cost of debt; often absent for companies with little debt."),
    Concept("diluted_shares", ("WeightedAverageNumberOfDilutedSharesOutstanding",),
            "duration", "shares", "warning",
            "Average diluted share count for the year. Cross-checked against yfinance."),

    # ---------------- Cash flow statement (duration) ----------------
    Concept("operating_cash_flow",
            ("NetCashProvidedByUsedInOperatingActivities",
             "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
            "duration", "USD", "error",
            "Cash from operations. FCF = operating cash flow - capex."),
    # PaymentsToAcquireOtherProductiveAssets is a retag of the same line, not an
    # extra amount: Workday and Twilio report identical values under both tags
    # in the years where both appear.
    Concept("capex",
            ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
             "PaymentsToAcquireOtherProductiveAssets"),
            "duration", "USD", "error",
            "Spending on servers, offices, etc. Reported as a positive outflow."),
    Concept("capitalized_software", ("PaymentsToDevelopSoftware", "PaymentsForSoftware"),
            "duration", "USD", "info",
            "Some SaaS firms capitalize dev costs; ignoring them overstates FCF."),
    # Deliberately excludes CapitalizedContractCostAmortization: that's
    # amortized sales commissions, an operating cost, and adding it back
    # would overstate EBITDA.
    Concept("depreciation_amortization",
            ("DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
             "DepreciationAndAmortization", "OtherDepreciationAndAmortization"),
            "duration", "USD", "warning",
            "Non-cash expense added back to EBIT to get EBITDA."),
    Concept("stock_based_comp",
            ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
            "duration", "USD", "warning",
            "Large for SaaS. A real cost to shareholders (dilution) even though it's non-cash."),

    # ---------------- Balance sheet (instant, at fiscal year-end) ----------------
    Concept("cash",
            ("CashAndCashEquivalentsAtCarryingValue",
             "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
            "instant", "USD", "error",
            "Subtracted from market cap to get enterprise value."),
    Concept("short_term_investments",
            ("ShortTermInvestments", "MarketableSecuritiesCurrent",
             "AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
            "instant", "USD", "info",
            "Liquid investments; usually treated like cash in EV."),
    # Debt is split three ways because filers disagree on presentation.
    # Stage 3 rule: total debt = debt_total if present, else current + noncurrent.
    # Never add debt_total to the other two; it already includes the current part.
    # Missing values are only "info" per year (companies issue and repay debt);
    # edgar.py raises one company-level warning if no debt is found in any year.
    Concept("debt_total", ("LongTermDebt",), "instant", "USD", "info",
            "Total borrowings including the portion due within a year."),
    Concept("debt_current",
            ("LongTermDebtCurrent", "ConvertibleNotesPayableCurrent", "ConvertibleDebtCurrent",
             "DebtCurrent"),
            "instant", "USD", "info",
            "Debt due within a year. Added to EV."),
    Concept("debt_noncurrent",
            ("LongTermDebtNoncurrent", "ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable",
             "LongTermNotesPayable", "SeniorLongTermNotes"),
            "instant", "USD", "info",
            "Long-term debt incl. convertible notes. Added to EV."),
    Concept("deferred_revenue_current",
            ("ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"),
            "instant", "USD", "info",
            "Cash collected for service not yet delivered; a leading growth signal for SaaS."),
    Concept("total_assets", ("Assets",), "instant", "USD", "error", "Balance sheet size."),
    Concept("total_equity",
            ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
            "instant", "USD", "error", "Book value of equity."),
)
