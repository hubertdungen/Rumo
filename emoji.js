/* Emojis automáticos: cada secção, linha, cofre e investimento recebe um emoji
   pelas palavras do nome. As regras são lidas por ordem e ganha a primeira que
   bate, por isso as mais específicas vêm primeiro («Manutenção Portátil» é
   portátil antes de ser manutenção). Português e inglês, que os estrangeirismos são o costume. O texto é comparado sem acentos e em
   minúsculas; \b no início deixa apanhar o começo das palavras («propina»
   apanha «Propinas»). Escolhas à mão vivem nas definições e mandam sobre isto.
   Teste: node test_emoji.js */
const EMOJI_RULES = [
  [/\bmaterial fac|\bpropina|\bfaculdade|\buniversidade|\bcurso|\bformacao|\bestudo|\bescola|\bexplicac|\bmanua(l|is) escola|\btuition|\bcollege|\buniversity|\bschool|\bcourses?\b|\bstud(y|ies)\b|\beducation|\bclass(es)?\b|\blessons?\b/, '🎓'],
  [/\bdesktop|\bmonitor|\bteclado|\bpc\b|\bscreen\b|\bkeyboard/, '🖥️'],
  [/\bportatil|\blaptop|\bcomputador|\binformatica|\bupgrade|\bsoftware|\bhardware|\bcomputer|\btech\b/, '💻'],
  [/\bimpress(ora|ao) 3d|\b3d\b|\bfilamento|\bresina|\bfatia\b|\bprinter|\b3d print/, '🖨️'],
  [/\bgoogle one|\bicloud|\bdropbox|\bcloud|\barmazenamento|\bstorage|\bbackup/, '☁️'],
  [/\bclaude\b|\bopenai|\bchatgpt|\bgemini|\bmidjourney|\bcopilot|\bai\b|\bia\b|\bbot\b|\binteligencia artificial|\bapi\b|\bllm\b|\bgpt\b|\bartificial intelligence/, '🤖'],
  [/\badobe|\bdesign|\bfigma|\bcanva|\bilustra/, '🎨'],
  [/\btlm\b|\btelemovel|\bgalaxy|\biphone|\bsmartphone|\btarifario|\bphones?\b|\bmobile\b|\bcell ?phone/, '📱'],
  [/\binternet|\brede\b|\bfibra|\bwi-?fi|\bmeo\b|\bvodafone|\bbroadband|\bnetwork/, '📶'],
  [/\bspotify|\bmusica|\bdeezer|\btidal|\bmusic\b|\bapple music|\bsoundcloud/, '🎵'],
  [/\bnetflix|\bhbo\b|\bdisney|\bprime video|\byoutube|\bstreaming|\btv\b|\btelevis/, '📺'],
  [/\bsubscric|\bassinatura|\bsubscription|\bmembership/, '🔁'],
  [/\bairsoft|\bhobb(y|ies)|\bpaintball|\btiro\b/, '🎯'],
  [/\bjogo|\bsteam\b|\bplaystation|\bps5|\bxbox|\bnintendo|\bjoystick|\bx-56|\bvalve|\bindex\b|\bvr\b|\bgaming|\bgames?\b|\bconsole|\bvideo ?games?/, '🎮'],
  [/\bcombustivel|\bgasolina|\bgasoleo|\bdiesel|\babastec|\bfuel|\bpetrol|\bgas station/, '⛽'],
  [/\bvia ?verde|\bportage|\bautoestrada|\bscut|\btolls?\b|\bhighway|\bmotorway/, '🛣️'],
  [/\bcarro|\bauto\b|\bbmw\b|\bpneu|\binspecao|\boficina|\bestacionamento|\bseguro auto|\biuc\b|\bcars?\b|\bvehicle|\bparking|\bmechanic|\btyres?\b|\btires?\b|\bcar insurance/, '🚗'],
  [/\btransporte|\bmetro\b|\bcomboio|\bcp\b|\buber\b|\bbolt\b|\bnavegante|\bpasse\b|\bautocarro|\btaxi\b|\btransport|\bbus\b|\btrains?\b|\bsubway|\bcommut/, '🚌'],
  [/\bcomer fora|\brestaurante|\bjantar|\balmoco|\btake ?away|\bglovo|\buber eats|\bpizza|\bcafe\b|\bpastelaria|\brestaurant|\bdinner|\blunch|\bbreakfast|\beating out|\bfood delivery|\bcoffee|\bsnacks?\b|\bfast food/, '🍽️'],
  [/\btalho|\bcarnes?\b|\bcarne\b|\bbife|\bfrango|\bporco|\bsalsicha|\bfiambre|\benchidos|\bpresunto|\bmeat|\bbutcher|\bbeef|\bchicken|\bpork/, '🥩'],
  [/\bpeixe|\bpeixaria|\bmarisco|\bbacalhau|\bsalmao|\batum\b|\bfish|\bseafood|\bshrimp|\bsalmon/, '🐟'],
  [/\bfruta|\bfruit|\bfrutaria/, '🍎'],
  [/\blegume|\bhortalica|\bvegetais|\bsalada|\bvegetables?\b|\bveg\b|\bsalad/, '🥦'],
  [/\bpadaria|\bpao\b|\bbolos?\b|\bbakery|\bbread/, '🥖'],
  [/\bleite|\blacticinio|\biogurte|\bmanteiga|\bovos?\b|\bmilk|\bdairy|\byogh?urt|\beggs?\b/, '🥛'],
  [/\bbebidas?\b|\bsumo|\brefrigerante|\bcerveja|\bbeverages?\b|\bsoda\b|\bjuice/, '🥤'],
  [/\bgelado|\bdoces?\b|\bchocolate|\bsobremesa|\bice cream|\bdesserts?\b|\bcandy|\bsweets/, '🍦'],
  [/\bmassa\b|\barroz|\bcereais|\bconservas|\bdespensa|\bpasta\b|\brice\b|\bpantry|\bcereals?\b/, '🍝'],
  [/\bsupermercado|\bmercearia|\bcontinente|\bpingo doce|\blidl|\baldi|\bauchan|\bcompras de casa|\bgrocer|\bsupermarket/, '🛒'],
  [/\bqueijo|\bvinho|\bcharcutaria|\bgourmet|\bcheese|\bwine/, '🧀'],
  [/\bsaida|\bnoite\b|\bbar\b|\bdiscoteca|\bcopos|\bfesta|\bconcerto|\bfestival|\bnight out|\bnightlife|\bpart(y|ies)\b|\bdrinks?\b|\bpubs?\b|\bbeers?\b/, '🍻'],
  [/\bcreatina|\bproteina|\bsuplemento|\bwhey|\bsupplement|\bprotein|\bcreatine/, '💪'],
  [/\broupa desport|\bequipamento|\bsportswear|\bactivewear|\bgym clothes/, '🎽'],
  [/\bcamisa|\b(?<!t-)shirts?\b|\bsuits?\b/, '👔'],
  [/\bcalca\b|\bcalcas|\bjeans|\bpants\b|\btrousers|\bjeans/, '👖'],
  [/\broupa|\bt-?shirt|\bcasaco|\bcamisola|\bsweat|\bcalcoes|\bmeias|\bcloth(es|ing)\b|\bapparel|\bhoodie/, '👕'],
  [/\bsapato|\bbota|\btenis|\bsapatilha|\bcalcado|\bshoes?\b|\bsneakers|\bboots?\b|\bfootwear/, '👟'],
  [/\bmensalidades desp|\bginasio|\bdesport|\btreino|\bfitness|\bpadel|\bfutebol|\bcorrida|\bgym\b|\bsports?\b|\bworkout|\btraining/, '🏋️'],
  [/\bbarbeiro|\bcabeleireiro|\bcorte de cabelo|\bestetica|\bbarber|\bhaircut|\bhairdress|\bgrooming/, '💈'],
  [/\bmedicac|\bfarmacia|\bmedicamento|\bremedio|\bmedication|\bpharmacy|\bmedicine|\bmeds\b|\bdrugstore/, '💊'],
  [/\bconsulta|\btratamento|\bmedico|\bdentista|\bimplant|\bexame|\bsaude|\bhospital|\bclinica|\bpsicolog|\bfisioterap|\boculos|\bdoctor|\bhealth|\bdentist|\bclinic|\btherapy|\bmedical|\bcheck-?up|\bglasses/, '🩺'],
  [/\bimposto|\btaxa|\birs\b|\bfinancas|\bestado\b|\bseguranca social|\bimi\b|\btax(es)?\b|\bgovernment|\bsocial security/, '🏛️'],
  [/\btribunal|\badvogad|\bmulta|\bcoima|\bprocesso|\bjudicial|\bcourt\b|\blawyer|\bfines?\b|\blegal\b|\battorney/, '⚖️'],
  [/\bcartao|\bcredito|\bprestac|\bdivida|\bemprestimo|\bmensalidade|\bleasing|\bcards?\b|\bcredit|\bloans?\b|\bdebts?\b|\binstal+ments?\b|\bmonthly payment/, '💳'],
  [/\blevant|\bnumerario|\bmultibanco|\batm\b|\bcash\b|\bwithdraw/, '🏧'],
  [/\bmb ?way/, '📲'],
  [/\btransf|\btransfer/, '🔄'],
  [/\bsantander|\brevolut|\bconta\b|\bbanco|\bcgd\b|\bmillennium|\bbpi\b|\bbank|\baccounts?\b/, '🏦'],
  [/\bcompras online|\bamazon|\baliexpress|\btemu\b|\bshein|\bencomenda|\bctt\b|\benvio|\bonline shopping|\bshipping|\bdelivery|\borders?\b|\bparcel/, '📦'],
  [/\bolx\b|\bvinted|\bvendas?\b|\bsales?\b|\bselling|\bresale|\bebay|\bmarketplace/, '🏷️'],
  [/\bdevoluc|\breembolso|\brefund|\breturns?\b/, '↩️'],
  [/\bprenda|\bpresente|\baniversario|\bnatal\b|\bgifts?\b|\bpresents?\b|\bbirthday|\bchristmas/, '🎁'],
  [/\bdiy\b|\bferramenta|\bbricolage|\bleroy|\bmaker|\btools?\b|\bhome improvement/, '🛠️'],
  [/\bmanut|\breparac|\barranjo|\bassistencia|\bmaintenance|\brepair|\bfix(es)?\b|\bservic(e|ing)\b/, '🔧'],
  [/\bentretenimento|\bcinema|\bteatro|\bbilhete|\blazer|\bhobby|\bentertainment|\bmovies?\b|\btheat(er|re)|\btickets?\b|\bleisure|\bhobbies|\bconcerts?\b/, '🎬'],
  [/\bluz\b|\beletric|\belectric|\benergia|\bedp\b|\bendesa|\biberdrola|\bgoldenergy|\bpower bill|\bpower\b/, '💡'],
  [/\bagua\b|\bepal|\baguas de|\bsmas\b|\bsaneamento|\bwater\b/, '💧'],
  [/\bgas\b|\bbotija|\bgas natural|\baquecimento|\bheating/, '🔥'],
  [/\blixo|\bresiduos|\brecolha|\bwaste|\btrash|\bgarbage/, '🗑️'],
  [/\bcondominio|\bcondo\b|\bhoa\b/, '🏢'],
  [/\blimpeza|\bdetergente|\bdrogaria|\bcleaning|\bcleaner/, '🧹'],
  [/\bseguro|\binsurance/, '🛡️'],
  [/\bcasa\b|\brenda\b|\bmobilia|\brecheio|\bobras?\b|\bikea|\bhabitacao|\bhome\b|\bhouse|\brent\b|\bmortgage|\butilit|\bfurniture|\bhousehold|\brenovat/, '🏠'],
  [/\bferias|\bviage|\bvoo\b|\bhotel|\bairbnb|\bbooking|\btravel|\bholidays?\b|\bvacation|\btrips?\b|\bflights?\b/, '✈️'],
  [/\banima|\bcao\b|\bgato|\bveterinari|\bpet\b|\bpets?\b|\bdogs?\b|\bcats?\b|\bvet\b/, '🐾'],
  [/\bfilho|\bcrianca|\bbebe\b|\bkids?\b|\bchild|\bbaby|\bsons?\b|\bdaughter/, '🧒'],
  [/\bacc?oes|\betf\b|\bbolsa\b|\bstock|\bshares\b|\bequities|\bstocks?\b/, '📈'],
  [/\binvesti|\binvest|\bportfolio/, '📊'],
  [/\bcrypto|\bcripto|\bbitcoin|\bbtc\b|\beth\b/, '🪙'],
  [/\bbens\b|\bcolec|\bouro\b|\brelogio|\bjoia|\bcollect|\bgold\b|\bwatch(es)?\b|\bjewel/, '💎'],
  [/\blucro|\bganho|\brendimento|\bprofit|\bincome|\bearnings|\bsalary|\bsalario|\bordenado|\brevenue/, '💵'],
  [/\bdungen|\bdungeon|\bempresa|\bnegocio|\bbusiness|\bcompany|\bstartup|\bside project/, '🧪'],
  [/\bpoupanca|\breserva|\bgeral\b|\bfundo de emergencia|\bcofre|\bsavings|\bemergency fund|\breserve|\bpiggy/, '💰'],
  [/\bacessorio|\bgadget|\baccessor/, '🎒'],
  [/\bimprevisto|\bemergencia|\bunexpected|\bemergenc/, '⚠️'],
  [/\bhistorico|\btotal importado|\bhistory|\bimported/, '🗂️'],
  [/\bdiversos|\bvarios|\bmisc|\bvarious|\bsundr/, '🎲'],
  [/\boutr[oa]s?\b|\bothers?\b/, '📎'],
  [/^-$/, '➖'],
  [/\bdespesa|\bexpenses?\b|\bbills?\b/, '🧾'],
];

// O que a grelha oferece para escolher: os do dicionário mais alguns de reserva.
const EMOJI_CHOICES = [...new Set(EMOJI_RULES.map(r => r[1]).concat(
  ['🏡','🛋️','🧹','🔌','💡','🚿','🍕','☕','🍷','🎂','🎉','📚','✏️','🧠','🎧','📷','🎸','⚽','🚴','🏊','🧘','🏖️','⛰️','🚀','🔋','🛡️','📄','🧮','🗓️','❤️','⭐','🔥']))];

const _normEmoji = s => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();

function autoEmoji(name, fallback) {
  const text = _normEmoji(name);
  for (const [re, emoji] of EMOJI_RULES) if (re.test(text)) return emoji;
  return fallback || '🏷️';
}

if (typeof module !== 'undefined') module.exports = { EMOJI_RULES, EMOJI_CHOICES, autoEmoji, _normEmoji };
