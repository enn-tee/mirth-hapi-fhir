// Mirth Connect — Source Transformer
//
// Paste this into the channel's Source > Transformer tab as a single JavaScript step.
// Mirth gives us `msg` as an E4X-style object holding the parsed HL7 v2 message,
// and `channelMap` for stashing values to use in the destination template.
//
// We extract PID fields, normalize v2 quirks (YYYYMMDD date, single-letter gender)
// into the shapes FHIR wants, and stash them for the destination connector.

// --- Identifiers and demographics -----------------------------------------
channelMap.put('PATIENT_MRN', msg['PID']['PID.3']['PID.3.1'].toString());
channelMap.put('LAST_NAME',   msg['PID']['PID.5']['PID.5.1'].toString());
channelMap.put('FIRST_NAME',  msg['PID']['PID.5']['PID.5.2'].toString());

// --- Date of birth: v2 ships YYYYMMDD, FHIR wants YYYY-MM-DD --------------
var dob = msg['PID']['PID.7']['PID.7.1'].toString();
channelMap.put(
    'DOB_FHIR',
    dob.length === 8
        ? dob.substring(0, 4) + '-' + dob.substring(4, 6) + '-' + dob.substring(6, 8)
        : ''   // a real channel would route empty DOB to an error queue; we're keeping it lossy on purpose so you can see what happens
);

// --- Gender: v2 single letter -> FHIR controlled vocabulary ---------------
var v2Gender = msg['PID']['PID.8']['PID.8.1'].toString();
var fhirGender = ({ M: 'male', F: 'female', O: 'other', U: 'unknown' })[v2Gender] || 'unknown';
channelMap.put('FHIR_GENDER', fhirGender);

// --- Address (PID-11 — repeating field; we use the first repetition) ------
var addr = msg['PID']['PID.11'][0] || msg['PID']['PID.11'];
channelMap.put('ADDR_LINE',  addr['PID.11.1'].toString());
channelMap.put('ADDR_CITY',  addr['PID.11.3'].toString());
channelMap.put('ADDR_STATE', addr['PID.11.4'].toString());
channelMap.put('ADDR_ZIP',   addr['PID.11.5'].toString());
